# SPDX-FileCopyrightText: 2026 Nicholas Gigliotti
# SPDX-License-Identifier: MIT

{ config, pkgs, ... }:
let
  helper = pkgs.writeScriptBin "surface-nvidia-power" ''
    #!${pkgs.python3}/bin/python3
    ${builtins.readFile ./surface-nvidia-power.py}
  '';
in
{
  assertions = [
    {
      assertion = config.hardware.nvidia.package.version == "595.71.05";
      message = "Surface NVIDIA power workaround must be reviewed for a different RM ABI.";
    }
  ];
  systemd.services.surface-nvidia-power = {
    description = "Restore Surface NVIDIA native power policy after RTD3 wake";
    wantedBy = [ "multi-user.target" ];
    unitConfig = {
      StartLimitIntervalSec = 600;
      StartLimitBurst = 20;
    };
    serviceConfig = {
      Type = "simple";
      # The main process waits with stat only; Type=simple completes startup.
      ExecStart = "${helper}/bin/surface-nvidia-power";
      Restart = "on-failure";
      RestartPreventExitStatus = 78;
      RestartSec = 3;
      TimeoutStopSec = 15;
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      PrivateTmp = true;
      DevicePolicy = "closed";
      DeviceAllow = [
        "/dev/char/195:255 rw"
        "/dev/char/195:0 rw"
      ];
    };
  };
}
