"""Hardware-free contract checks; no real sysfs reads or NVIDIA ioctls."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, call, patch

spec = importlib.util.spec_from_file_location(
    "surface_power_candidate", Path(__file__).with_name("surface-nvidia-power.py")
)
power = importlib.util.module_from_spec(spec)
spec.loader.exec_module(power)


class ResourceTests(unittest.TestCase):
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


class PolicyTests(unittest.TestCase):
    def test_idle_loop_and_shutdown_never_open_gpu(self):
        stopped = MagicMock()
        stopped.is_set.side_effect = [False, False, True]
        with patch.object(power, "verify_hardware"), \
             patch.object(power, "text", return_value="suspended"), \
             patch.object(power, "stopping", stopped), \
             patch.object(power, "log"), \
             patch.object(power, "rm_device") as rm:
            power.main()
            rm.assert_not_called()
            self.assertEqual(stopped.wait.call_args_list, [call(0.2), call(0.2)])

    def test_live_source_is_reread_after_allocation(self):
        for online, expected in (("0", 1), ("1", 0)):
            with self.subTest(online=online), \
                 patch.object(power, "rm_device") as rm, \
                 patch.object(power, "text", return_value=online), \
                 patch.object(power, "control", side_effect=[0, 0, expected]) as control, \
                 patch.object(power, "log"):
                rm.return_value.__enter__.return_value = (10, 0xA, 0x1002)
                power.apply_power_source("1" if online == "0" else "0")
                self.assertEqual(control.call_args_list, [
                    call(10, 0xA, 0x1002, 0x2080205B, expected),
                    call(10, 0xA, 0x1002, 0x20802092, 0),
                    call(10, 0xA, 0x1002, 0x2080205A, 0xFFFFFFFF),
                ])
                rm.return_value.__exit__.assert_called_once()

    def test_source_readback_mismatch_fails_and_exits_client(self):
        with patch.object(power, "rm_device") as rm, \
             patch.object(power, "text", return_value="1"), \
             patch.object(power, "control", side_effect=[0, 0, 1]):
            rm.return_value.__enter__.return_value = (10, 0xA, 0x1002)
            with self.assertRaisesRegex(RuntimeError, "readback mismatch"):
                power.apply_power_source("1")
            rm.return_value.__exit__.assert_called_once()

    def test_unknown_initial_source_does_not_allocate(self):
        with patch.object(power, "rm_device") as rm:
            with self.assertRaisesRegex(RuntimeError, "Unknown AC"):
                power.apply_power_source("unknown")
            rm.assert_not_called()


class GuardTests(unittest.TestCase):
    @staticmethod
    def hardware_text(path):
        name = str(path)
        values = {
            "product_name": "Surface Laptop Studio 2",
            "vendor": "0x10de", "device": "0x28a0",
            "subsystem_vendor": "0x1414", "subsystem_device": "0x0083",
            "version": "NVIDIA Open Kernel Module 595.71.05",
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


if __name__ == "__main__":
    unittest.main()
