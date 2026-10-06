"""Hardware-free contract checks; no real sysfs reads or NVIDIA ioctls."""
import configparser
import importlib.util
import json
from pathlib import Path
import shutil
import selectors
import signal
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

spec = importlib.util.spec_from_file_location(
    "surface_power_candidate", Path(__file__).with_name("surface-nvidia-power.py")
)
power = importlib.util.module_from_spec(spec)
spec.loader.exec_module(power)


class HardwareFreeTests(unittest.TestCase):
    def setUp(self):
        # Catch missing mocks even on a host where the requested path exists.
        for name in ("read_text", "read_bytes", "stat", "glob", "iterdir"):
            original = getattr(Path, name)
            def guarded(path, *args, _original=original, **kwargs):
                if str(path).startswith(("/sys/", "/proc/driver/nvidia/", "/dev/nvidia")):
                    raise AssertionError(f"Unexpected host hardware read: {path}")
                return _original(path, *args, **kwargs)
            mock = patch.object(Path, name, guarded)
            mock.start()
            self.addCleanup(mock.stop)

        for name in ("open", "scandir"):
            original = getattr(power.os, name)
            def guarded(path, *args, _original=original, **kwargs):
                if isinstance(path, (str, bytes, Path)) and power.os.fsdecode(path).startswith(
                    ("/sys/", "/proc/driver/nvidia/", "/dev/nvidia")
                ):
                    raise AssertionError(f"Unexpected host hardware access: {path}")
                return _original(path, *args, **kwargs)
            mock = patch.object(power.os, name, guarded)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(power.fcntl, "ioctl", side_effect=AssertionError("Unexpected real ioctl"))
        mock.start()
        self.addCleanup(mock.stop)


class MainLoopTests(HardwareFreeTests):
    def setUp(self):
        super().setUp()
        mock = patch.object(power, "stopping", False)
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(power, "verify_identity")
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(power, "wait_for_devices", return_value=True)
        mock.start()
        self.addCleanup(mock.stop)


class ResourceTests(HardwareFreeTests):
    def setUp(self):
        super().setUp()
        mock = patch.object(power, "verify_driver")
        mock.start()
        self.addCleanup(mock.stop)

    def test_driver_change_during_open_closes_fds_before_any_ioctl(self):
        with patch.object(power, "verify_driver", side_effect=[None, power.GuardError("changed ABI")]), \
             patch.object(power.os, "open", side_effect=[10, 11]), \
             patch.object(power.os, "close") as close, \
             patch.object(power, "ioctl") as ioctl:
            with self.assertRaises(power.GuardError):
                with power.rm_device():
                    self.fail("Changed driver must not yield")
            ioctl.assert_not_called()
            self.assertEqual(close.call_args_list, [call(11), call(10)])

    def test_rm_client_and_fds_released_when_work_fails(self):
        with patch.object(power.os, "open", side_effect=[10, 11]), \
             patch.object(power.os, "close") as close, \
             patch.object(power, "allocate", side_effect=[0xA, 0x1001, 0x1002]), \
             patch.object(power, "ioctl") as ioctl:
            with self.assertRaisesRegex(RuntimeError, "work failed"):
                with power.rm_device():
                    raise RuntimeError("work failed")
            free_call = ioctl.call_args_list[-1]
            self.assertEqual(free_call.args[:2], (10, 0x29))
            self.assertEqual(free_call.args[2].old, 0xA)
            self.assertEqual(close.call_args_list, [call(11), call(10)])

    def test_client_released_when_device_allocation_fails(self):
        with patch.object(power.os, "open", side_effect=[10, 11]), \
             patch.object(power.os, "close") as close, \
             patch.object(power, "allocate", side_effect=[0xA, RuntimeError("alloc failed")]), \
             patch.object(power, "ioctl") as ioctl:
            with self.assertRaisesRegex(RuntimeError, "alloc failed"):
                with power.rm_device():
                    self.fail("failed allocation must not yield")
            self.assertEqual(ioctl.call_args.args[1], 0x29)
            self.assertEqual(ioctl.call_args.args[2].old, 0xA)
            self.assertEqual(close.call_args_list, [call(11), call(10)])

    def test_control_fd_closed_when_gpu_open_fails(self):
        with patch.object(power.os, "open", side_effect=[10, OSError("open failed")]), \
             patch.object(power.os, "close") as close:
            with self.assertRaisesRegex(OSError, "open failed"):
                with power.rm_device():
                    self.fail("failed open must not yield")
            close.assert_called_once_with(10)

    def test_fds_close_even_when_rm_free_fails(self):
        def ioctl_side_effect(fd, nr, params):
            if nr == 0x29:
                raise RuntimeError("free failed")

        with patch.object(power.os, "open", side_effect=[10, 11]), \
             patch.object(power.os, "close") as close, \
             patch.object(power, "allocate", side_effect=[0xA, 0x1001, 0x1002]), \
             patch.object(power, "ioctl", side_effect=ioctl_side_effect):
            with self.assertRaisesRegex(RuntimeError, "free failed"):
                with power.rm_device():
                    pass
            self.assertEqual(close.call_args_list, [call(11), call(10)])


class PolicyTests(MainLoopTests):
    def test_system_resume_reapplies_policy_without_runtime_epoch_change(self):
        stopped = MagicMock()
        stopped.__bool__.side_effect = [False, False, False, False, True]
        with patch.object(power, "verify_hardware"), \
             patch.object(power, "read_power_source", return_value="1"), \
             patch.object(power, "stopping", stopped), \
             patch.object(power.time, "sleep"), \
             patch.object(power, "sleep_offset_bounds", side_effect=[(0, 0.001), (30, 30.001), (30, 30.001)]), \
             patch.object(power, "active_epoch", return_value=("100", "1")), \
             patch.object(power, "apply_power_source", return_value="1") as apply, \
             patch.object(power, "restore_firmware_restriction"), \
             patch.object(power, "log") as log:
            self.assertEqual(power.main(), 0)
            self.assertEqual(apply.call_count, 2)
            self.assertEqual(sum(c.kwargs.get("event") == "system_resume" for c in log.call_args_list), 1)

    def test_clock_read_scheduling_delay_does_not_reapply(self):
        stopped = MagicMock()
        stopped.__bool__.side_effect = [False, False, False, False, True]
        with patch.object(power, "verify_hardware"), \
             patch.object(power, "read_power_source", return_value="1"), \
             patch.object(power, "stopping", stopped), \
             patch.object(power.time, "sleep"), \
             patch.object(power, "sleep_offset_bounds", side_effect=[(0, 0.001), (-3, 0), (0, 3)]), \
             patch.object(power, "active_epoch", return_value=("100", "1")), \
             patch.object(power, "apply_power_source", return_value="1") as apply, \
             patch.object(power, "restore_firmware_restriction"), \
             patch.object(power, "log"):
            self.assertEqual(power.main(), 0)
            apply.assert_called_once()

    def test_sleep_offset_brackets_clock_sample(self):
        with patch.object(power.time, "monotonic", side_effect=[10, 12]), \
             patch.object(power.time, "clock_gettime", return_value=42) as clock:
            self.assertEqual(power.sleep_offset_bounds(), (30, 32))
            clock.assert_called_once_with(power.time.CLOCK_BOOTTIME)

    def test_later_driver_guard_failure_is_terminal_and_restore_is_safe(self):
        with patch.object(power, "verify_hardware"), \
             patch.object(power, "read_power_source", return_value="1"), \
             patch.object(power, "active_epoch", return_value=("100", "1")), \
             patch.object(power, "active_gpu_epoch", return_value="100"), \
             patch.object(power, "verify_driver", side_effect=power.GuardError("changed ABI")), \
             patch.object(power.os, "open") as opened, \
             patch.object(power, "log") as log:
            self.assertEqual(power.main(), power.GUARD_EXIT_STATUS)
            opened.assert_not_called()
            self.assertEqual([entry.kwargs["event"] for entry in log.call_args_list],
                             ["started", "rejected", "restore_failed", "stopped"])

    def test_idle_loop_and_shutdown_never_open_gpu(self):
        stopped = MagicMock()
        stopped.__bool__.side_effect = [False, False, False, True]
        with patch.object(power, "verify_hardware"), \
             patch.object(power, "read_power_source", return_value="1"), \
             patch.object(power, "text", return_value="suspended"), \
             patch.object(power, "stopping", stopped), \
             patch.object(power.time, "sleep") as sleep, \
             patch.object(power, "log"), \
             patch.object(power, "rm_device") as rm:
            power.main()
            rm.assert_not_called()
            self.assertEqual(sleep.call_args_list, [call(power.INTERVAL), call(power.INTERVAL)])

    def test_live_source_is_reread_after_allocation(self):
        for online, expected in (("0", 1), ("1", 0)):
            with self.subTest(online=online), \
                 patch.object(power, "rm_device") as rm, \
                 patch.object(power, "text", return_value=online), \
                 patch.object(power, "control", side_effect=[0, 0, expected]) as control, \
                 patch.object(power, "log"):
                rm.return_value.__enter__.return_value = (10, 0xA, 0x1002)
                applied = power.apply_power_source()
                self.assertEqual(applied, online)
                self.assertEqual(control.call_args_list, [
                    call(10, 0xA, 0x1002, 0x2080205B, expected),
                    call(10, 0xA, 0x1002, 0x20802092, 0),
                    call(10, 0xA, 0x1002, 0x2080205A, 0xFFFFFFFF),
                ])
                rm.return_value.__exit__.assert_called_once()

    def test_loop_caches_source_applied_after_allocation(self):
        for initial, applied in (("1", "0"), ("0", "1")):
            for next_source in (initial, applied):
                with self.subTest(initial=initial, applied=applied, next_source=next_source):
                    # The source changes during RM allocation. If it changes
                    # back before the next poll, correct it; if it stays at
                    # the applied value, avoid another unnecessary GPU open.
                    sources = [applied, initial] if next_source == initial else [applied]
                    stopped = MagicMock()
                    stopped.__bool__.side_effect = [False, False, False, False, True]
                    with patch.object(power, "verify_hardware"), \
                         patch.object(power, "active_epoch", side_effect=[
                             ("100", initial), ("100", next_source), ("100", next_source)
                         ]), \
                         patch.object(power, "text", side_effect=[initial, *sources]), \
                         patch.object(power, "stopping", stopped), \
                         patch.object(power.time, "sleep"), \
                         patch.object(power, "log"), \
                         patch.object(power, "restore_firmware_restriction"), \
                         patch.object(power, "rm_device") as rm, \
                         patch.object(power, "control", side_effect=[
                             value for source in sources
                             for value in (0, 0, 0 if source == "1" else 1)
                         ]) as control:
                        rm.return_value.__enter__.return_value = (10, 0xA, 0x1002)
                        power.main()
                        self.assertEqual(rm.call_count, len(sources))
                        self.assertEqual(rm.return_value.__exit__.call_count, len(sources))
                        self.assertEqual(control.call_args_list[::3], [
                            call(10, 0xA, 0x1002, 0x2080205B, 0 if source == "1" else 1)
                            for source in sources
                        ])

    def test_unknown_reread_source_exits_without_policy_controls(self):
        with patch.object(power, "rm_device") as rm, \
             patch.object(power, "text", return_value="unknown"), \
             patch.object(power, "control") as control:
            rm.return_value.__enter__.return_value = (10, 0xA, 0x1002)
            with self.assertRaisesRegex(RuntimeError, "Unknown AC"):
                power.apply_power_source()
            control.assert_not_called()
            rm.return_value.__exit__.assert_called_once()

    def test_source_readback_mismatch_fails_and_exits_client(self):
        with patch.object(power, "rm_device") as rm, \
             patch.object(power, "text", return_value="1"), \
             patch.object(power, "control", side_effect=[0, 0, 1]):
            rm.return_value.__enter__.return_value = (10, 0xA, 0x1002)
            with self.assertRaisesRegex(RuntimeError, "readback mismatch"):
                power.apply_power_source()
            rm.return_value.__exit__.assert_called_once()

    def test_unknown_initial_source_does_not_allocate(self):
        with patch.object(power, "verify_hardware"), \
             patch.object(power, "text", return_value="unknown"), \
             patch.object(power.time, "sleep", side_effect=lambda _: power.request_stop(signal.SIGTERM, None)), \
             patch.object(power, "stopping", False), \
             patch.object(power, "log"), \
             patch.object(power, "rm_device") as rm:
            self.assertEqual(power.main(), 0)
            rm.assert_not_called()

    def test_shutdown_unknown_or_missing_source_restores_only_auxiliary_p4(self):
        for source in ("unknown", FileNotFoundError("adapter gone"), OSError("read failed")):
            with self.subTest(source=source), \
                 patch.object(power, "active_gpu_epoch", return_value="100"), \
                 patch.object(power, "text", side_effect=[source]), \
                 patch.object(power, "rm_device") as rm, \
                 patch.object(power, "control") as control, \
                 patch.object(power, "log") as log:
                rm.return_value.__enter__.return_value = (10, 0xA, 0x1002)
                power.restore_firmware_restriction()
                control.assert_called_once_with(10, 0xA, 0x1002, 0x20802092, 4)
                rm.return_value.__exit__.assert_called_once()
                self.assertEqual(log.call_args_list[0].kwargs["event"], "restore_source_unavailable")

    def test_shutdown_reports_valid_source_before_auxiliary_p4(self):
        for source, state in (("0", 1), ("1", 0)):
            with self.subTest(source=source), \
                 patch.object(power, "active_gpu_epoch", return_value="100"), \
                 patch.object(power, "text", return_value=source), \
                 patch.object(power, "rm_device") as rm, \
                 patch.object(power, "control") as control, \
                 patch.object(power, "log"):
                rm.return_value.__enter__.return_value = (10, 0xA, 0x1002)
                power.restore_firmware_restriction()
                self.assertEqual(control.call_args_list, [
                    call(10, 0xA, 0x1002, 0x2080205B, state),
                    call(10, 0xA, 0x1002, 0x20802092, 4),
                ])

    def test_shutdown_source_control_failure_still_attempts_auxiliary_p4(self):
        with patch.object(power, "active_gpu_epoch", return_value="100"), \
             patch.object(power, "text", return_value="1"), \
             patch.object(power, "rm_device") as rm, \
             patch.object(power, "control", side_effect=[RuntimeError("source failed"), 0]) as control, \
             patch.object(power, "log") as log:
            rm.return_value.__enter__.return_value = (10, 0xA, 0x1002)
            power.restore_firmware_restriction()
            self.assertEqual(control.call_args_list, [
                call(10, 0xA, 0x1002, 0x2080205B, 0),
                call(10, 0xA, 0x1002, 0x20802092, 4),
            ])
            self.assertEqual(log.call_args_list[0].kwargs["event"], "restore_source_failed")

    def test_failed_restore_preserves_original_error_and_logs_stop(self):
        stopped = MagicMock()
        stopped.__bool__.return_value = False
        with patch.object(power, "verify_hardware"), \
             patch.object(power, "read_power_source", return_value="1"), \
             patch.object(power, "stopping", stopped), \
             patch.object(power.time, "sleep"), \
             patch.object(power, "active_epoch", side_effect=RuntimeError("original failure")), \
             patch.object(power, "restore_firmware_restriction", side_effect=OSError("restore failure")), \
             patch.object(power, "log") as log:
            with self.assertRaisesRegex(RuntimeError, "original failure"):
                power.main()
            self.assertEqual([entry.kwargs["event"] for entry in log.call_args_list],
                             ["started", "restore_failed", "stopped"])

    def test_missing_source_before_allocation_does_not_open_gpu(self):
        with patch.object(power, "verify_hardware"), \
             patch.object(power, "text", side_effect=FileNotFoundError("adapter gone")), \
             patch.object(power.time, "sleep", side_effect=lambda _: power.request_stop(signal.SIGTERM, None)), \
             patch.object(power, "stopping", False), \
             patch.object(power, "log"), \
             patch.object(power, "rm_device") as rm:
            self.assertEqual(power.main(), 0)
            rm.assert_not_called()


class GuardTests(MainLoopTests):
    def setUp(self):
        identity = power.verify_identity
        super().setUp()
        power.verify_identity.side_effect = identity

    def test_guard_failures_return_nonretryable_status_without_gpu_access(self):
        for error in (RuntimeError("unsupported"), OSError("guard read failed")):
            with self.subTest(error=error), \
                 patch.object(power, "verify_identity"), \
                 patch.object(power, "verify_hardware", side_effect=error), \
                 patch.object(power, "rm_device") as rm, \
                 patch.object(power, "log") as log:
                self.assertEqual(power.main(), power.GUARD_EXIT_STATUS)
                rm.assert_not_called()
                log.assert_called_once_with(event="rejected", error=str(error))

    def test_unexpected_abi_sizes_refused_before_sysfs_access(self):
        with patch.object(power.c, "sizeof", return_value=1), \
             patch.object(power, "text") as text:
            with self.assertRaisesRegex(RuntimeError, "Unexpected RM parameter sizes"):
                power.verify_hardware()
            text.assert_not_called()

    def test_source_failures_wait_until_stopped_after_identity_checks(self):
        for error in (FileNotFoundError("adapter late"), OSError("transient EC error"),
                      RuntimeError("Unknown AC state")):
            with self.subTest(error=error), \
                 patch.object(power, "verify_identity"), \
                 patch.object(power, "verify_hardware") as verify, \
                 patch.object(power, "read_power_source", side_effect=error), \
                 patch.object(power.time, "sleep", side_effect=lambda _: power.request_stop(signal.SIGTERM, None)), \
                 patch.object(power, "stopping", False), \
                 patch.object(power, "rm_device") as rm, \
                 patch.object(power, "log") as log:
                self.assertEqual(power.main(), 0)
                verify.assert_called_once()
                rm.assert_not_called()
                self.assertEqual(log.call_args_list, [call(event="source_unavailable", error=str(error)),
                                                       call(event="stopped")])

    @staticmethod
    def hardware_text(path):
        name = str(path)
        values = {
            "product_name": "Surface Laptop Studio 2",
            "vendor": "0x10de", "device": "0x28a0",
            "subsystem_vendor": "0x1414", "subsystem_device": "0x0083",
            "version": "NVIDIA Open Kernel Module " + power.DRIVER,
            "information": "Device Minor: 0",
        }
        return values[name.rsplit("/", 1)[-1]]

    def test_other_laptop_refused_before_gpu_access(self):
        with patch.object(power, "text", return_value="Other Laptop") as text, \
             patch.object(power.os, "open") as opened:
            with self.assertRaisesRegex(RuntimeError, "Unsupported laptop"):
                power.verify_hardware()
            self.assertEqual(text.call_count, 1)
            opened.assert_not_called()

    def test_other_driver_refused(self):
        def changed_driver(path):
            return "615.71.09" if str(path).endswith("version") else self.hardware_text(path)
        with patch.object(power, "text", side_effect=changed_driver):
            with self.assertRaisesRegex(RuntimeError, "RM ABI"):
                power.verify_hardware()

    def test_other_minor_and_version_prefix_refused_without_gpu_access(self):
        for version, info in ((power.DRIVER, "Device Minor: 1"),
                              (power.DRIVER + ".1", "Device Minor: 0")):
            def driver_text(path):
                return version if str(path).endswith("version") else info
            with self.subTest(version=version, info=info), \
                 patch.object(power, "text", side_effect=driver_text), \
                 patch.object(power.os, "open") as opened:
                with self.assertRaises(power.GuardError):
                    with power.rm_device():
                        self.fail("An unvalidated device must not be yielded")
                opened.assert_not_called()

    def test_bound_sgpc_driver_refused(self):
        sgpc = MagicMock()
        sgpc.exists.return_value = True
        sgpc.__truediv__.return_value.exists.return_value = True
        with patch.object(power, "text", side_effect=self.hardware_text), \
             patch.object(power, "SGPC", sgpc):
            with self.assertRaisesRegex(RuntimeError, "already controlled"):
                power.verify_hardware()

    def test_missing_known_firmware_refused(self):
        sgpc = MagicMock()
        sgpc.exists.return_value = True
        sgpc.__truediv__.return_value.exists.return_value = False
        with patch.object(power, "text", side_effect=self.hardware_text), \
             patch.object(power, "SGPC", sgpc), \
             patch.object(power, "Path") as path:
            path.return_value.glob.return_value = []
            with self.assertRaisesRegex(RuntimeError, "GPU firmware differs"):
                power.verify_hardware()


class ServiceStartupTests(HardwareFreeTests):
    def test_main_stop_during_node_wait_skips_remaining_guards(self):
        with patch.object(power, "verify_identity") as identity, \
             patch.object(power, "wait_for_devices", return_value=False), \
             patch.object(power, "verify_hardware") as verify, \
             patch.object(power, "wait_for_source") as source, \
             patch.object(power, "log") as log:
            self.assertEqual(power.main(), 0)
            identity.assert_called_once()
            verify.assert_not_called()
            source.assert_not_called()
            log.assert_called_once_with(event="stopped")

    def test_wrong_laptop_is_rejected_before_device_wait(self):
        with patch.object(power, "text", return_value="Other Laptop"), \
             patch.object(power, "wait_for_devices") as wait, \
             patch.object(power, "log"):
            self.assertEqual(power.main(), power.GUARD_EXIT_STATUS)
            wait.assert_not_called()

    def test_late_source_wait_recovers_without_gpu_access_or_restarts(self):
        with patch.object(power, "stopping", False), \
             patch.object(power, "read_power_source", side_effect=[FileNotFoundError("late"),
                 RuntimeError("unknown"), OSError("temporary"), "1"]), \
             patch.object(power.time, "sleep") as sleep, \
             patch.object(power.os, "open") as opened, \
             patch.object(power, "log") as log:
            self.assertTrue(power.wait_for_source())
            self.assertEqual(sleep.call_count, 3)
            opened.assert_not_called()
            log.assert_called_once_with(event="source_unavailable", error="late")

    def test_hardware_guard_blocks_unmocked_open_and_directory_scan(self):
        for operation in (lambda: power.os.open("/dev/nvidiactl", power.os.O_RDWR),
                          lambda: power.os.scandir("/sys/firmware/acpi/tables"),
                          lambda: list(Path("/sys/firmware/acpi/tables").glob("SSDT*"))):
            with self.assertRaisesRegex(AssertionError, "Unexpected host hardware"):
                operation()

    def test_waits_for_both_character_devices_without_opening_them(self):
        with tempfile.TemporaryDirectory() as directory:
            nodes = [Path(directory) / name for name in ("nvidiactl", "nvidia0")]
            phases = []
            def advance(interval):
                self.assertEqual(interval, power.INTERVAL)
                phases.append(len(phases))
                if len(phases) == 1:
                    nodes[0].symlink_to("/dev/null")
                elif len(phases) == 2:
                    nodes[1].write_text("")
                elif len(phases) == 3:
                    nodes[1].unlink()
                    nodes[1].symlink_to("/dev/null")
                else:
                    self.fail("Did not accept both character devices")
            with patch.object(power, "NVIDIA_NODES", nodes), \
                 patch.object(power, "stopping", False), \
                 patch.object(power.time, "sleep", side_effect=advance), \
                 patch.object(power.os, "open") as opened:
                self.assertTrue(power.wait_for_devices())
                self.assertEqual(len(phases), 3)
                opened.assert_not_called()

    def test_startup_wait_stops_on_signal_flag(self):
        with patch.object(power, "stopping", False), \
             patch.object(power, "NVIDIA_NODES", [Path("/nonexistent-candidate-test-node")]), \
             patch.object(power.time, "sleep", side_effect=lambda _: power.request_stop(signal.SIGTERM, None)):
            self.assertFalse(power.wait_for_devices())
            self.assertTrue(power.stopping)

    @staticmethod
    def conventional_unit():
        text = Path(__file__).with_name("surface-nvidia-power.service").read_text()
        unit = configparser.ConfigParser(strict=False)
        unit.optionxform = str
        unit.read_string(text)
        return unit

    def check_policy(self, unit, config):
        self.assertFalse(any(key.startswith("Condition") for key in unit))
        self.assertEqual(int(unit["StartLimitIntervalSec"]), 600)
        self.assertEqual(int(unit["StartLimitBurst"]), 20)
        self.assertEqual(config["Restart"], "on-failure")
        self.assertEqual(int(config["RestartSec"]), 3)
        self.assertEqual(int(config["RestartPreventExitStatus"]), power.GUARD_EXIT_STATUS)
        self.assertEqual(config["Type"], "simple")
        self.assertNotIn("ExecStartPre", config)
        self.assertNotIn("TimeoutStartSec", config)

    def test_conventional_unit_startup_wait_and_bounded_restart_policy(self):
        unit = self.conventional_unit()
        self.check_policy(unit["Unit"], unit["Service"])

    @unittest.skipUnless(shutil.which("nix-instantiate"), "Nix evaluator unavailable")
    def test_nixos_module_policy_parity_and_driver_assertion(self):
        module = Path(__file__).with_name("surface-nvidia-power.nix").resolve()
        expression = """
          let module = import (/. + %s) {
            config.hardware.nvidia.package.version = %s;
            pkgs = {
              python3 = "/unused/python3";
              writeScriptBin = name: script: "/unused/helper";
            };
          }; in {
            inherit (module) assertions;
            service = module.systemd.services.surface-nvidia-power;
          }
        """ % (json.dumps(str(module)), json.dumps(power.DRIVER))
        result = subprocess.run(
            ["nix-instantiate", "--eval", "--strict", "--json", "--expr", expression],
            check=True, capture_output=True, text=True
        )
        evaluated = json.loads(result.stdout)
        self.assertTrue(all(item["assertion"] for item in evaluated["assertions"]))
        service = evaluated["service"]
        config = service["serviceConfig"]
        self.check_policy(service["unitConfig"], config)
        unit = self.conventional_unit()
        # Parse repeated DeviceAllow separately; ConfigParser retains the last.
        unit_text = Path(__file__).with_name("surface-nvidia-power.service").read_text()
        conventional = dict(unit["Service"])
        conventional["DeviceAllow"] = [line.split("=", 1)[1] for line in unit_text.splitlines()
                                       if line.startswith("DeviceAllow=")]
        for settings in (conventional, config):
            settings["ExecStart"] = "<packaged helper>"
            for name, value in list(settings.items()):
                if isinstance(value, bool):
                    settings[name] = str(value).lower()
                elif isinstance(value, int):
                    settings[name] = str(value)
        self.assertEqual(conventional, config)


class SignalTests(HardwareFreeTests):
    def test_sigterm_exits_idle_loop_and_logs_stop(self):
        helper = Path(__file__).with_name("surface-nvidia-power.py").resolve()
        script = """
import importlib.util, signal
spec = importlib.util.spec_from_file_location("candidate", %s)
power = importlib.util.module_from_spec(spec)
spec.loader.exec_module(power)
power.verify_identity = lambda: None
power.verify_hardware = lambda: None
power.wait_for_devices = lambda: True
power.read_power_source = lambda: "1"
power.active_epoch = lambda: None
power.restore_firmware_restriction = lambda: None
signal.signal(signal.SIGTERM, power.request_stop)
raise SystemExit(power.main())
""" % json.dumps(str(helper))
        process = subprocess.Popen([sys.executable, "-B", "-c", script],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), "Dummy loop did not start")
                self.assertEqual(json.loads(process.stdout.readline())["event"], "started")
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, errors)
            self.assertEqual(json.loads(output.strip())["event"], "stopped")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_unknown_argument_rejected_before_hardware_access(self):
        helper = Path(__file__).with_name("surface-nvidia-power.py").resolve()
        script = """
import ast, pathlib, sys
path = %s
tree = ast.parse(pathlib.Path(path).read_text(), filename=path)
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "main":
        node.body = ast.parse('raise AssertionError("main called")').body
ast.fix_missing_locations(tree)
sys.argv = [path, "--wait-for-device"]
exec(compile(tree, path, "exec"), {"__name__": "__main__"})
""" % json.dumps(str(helper))
        result = subprocess.run([sys.executable, "-B", "-c", script],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
