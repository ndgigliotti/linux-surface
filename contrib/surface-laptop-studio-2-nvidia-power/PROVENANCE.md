<!-- SPDX-FileCopyrightText: 2026 Nicholas Gigliotti -->
<!-- SPDX-License-Identifier: MIT -->

# Sources and validation scope

The example was contributed by Nicholas Gigliotti. The helper and Nix module
started from the local Surface Laptop Studio 2 configuration used for the
2026-10-02 physical tests. Initial contribution `ee41701108271adc7171a77c02e72da992738b9c`
copied those files and the NVIDIA patch unchanged. Subsequent commits correct
source caching, startup, shutdown and system-sleep detection and add guards.
Those revisions have not been installed or physically tested.

The RM layouts and control constants follow NVIDIA's published 595.71.05 sources:

- `src/common/sdk/nvidia/inc/nvos.h`: allocation, control and free parameters.
- `src/common/sdk/nvidia/inc/class/cl0080.h`: device allocation parameters.
- `src/common/sdk/nvidia/inc/ctrl/ctrl2080/ctrl2080perf.h`: source/auxiliary controls.
- `src/nvidia/arch/nvalloc/unix/include/nv_escape.h`: RM ioctl numbers.
- `kernel-open/common/inc/nv-ioctl-numbers.h`: ioctl magic and register-FD number.
- `src/common/sdk/nvidia/inc/class/cl2080.h`: subdevice class/allocation parameters.
- `kernel-open/nvidia/nv-acpi.c`: the query fallback's target source.

[NVIDIA's 595.71.05 source](https://github.com/NVIDIA/open-gpu-kernel-modules/tree/595.71.05)
retains its own MIT notices; see [NVIDIA-LICENSE](NVIDIA-LICENSE). The helper
also carries the RM declaration notices and permission text in its own header
for installed copies. The bundled patch
is the original narrow fallback, with the provider/PM defect disclosed in the
README. A separate newer-driver redesign is not a backport or validation of it.

The counterfactual and clpeak logs and `gen16-postboot-validation.json` are
historical machine-generated diagnostics from the reported test configuration.
They are retained byte-for-byte. Battery-transition evidence predates the
source/startup/shutdown revisions; historical JSON scope text describes the
original helper. The published [Surface report](https://github.com/linux-surface/linux-surface/issues/2185#issuecomment-5962487523)
and [NVIDIA report](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/483#issuecomment-5962485298)
provide the original context.

Implementation and documentation used Codex assistance. Claude Code performed
independent source reviews; findings and subsequent corrections were assessed
locally. Claude Opus 5.5 reviewed the full licensed checkpoint `f44f799` at xhigh.
The `aa4bd9e` follow-up adds suspend-attempt invalidation, startup source backoff,
tests and documentation corrections. A focused Opus 5.5 xhigh re-review confirmed
the prior dispositions and found no new P1/P2/P3 defects. Subsequent test and
review-record corrections leave the runtime helper unchanged from that head.
The 41 mocked tests, static unit checks and disposable user-manager lifecycle
checks do not execute GPU controls or validate full system-manager hardening,
RM/GSP behavior or system suspend. Neither AI review nor a passing mock replaces
those hardware checks. No same-version closed-module comparison was performed.

[LICENSE](LICENSE) covers only `surface-nvidia-power.py`, `surface-nvidia-power.nix`,
`surface-nvidia-power.service`, `test_surface_nvidia_power.py`, `README.md` and
this document. NVIDIA declarations/patch retain their original MIT terms.
Historical diagnostic outputs are factual records provided without an additional
license grant here; no earlier output license is claimed, and third-party tool
output is not assigned a new license. Their redistribution treatment remains a
maintainer decision. Other repository licenses are unchanged.
