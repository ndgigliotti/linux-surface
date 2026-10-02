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
    after = [ "systemd-udev-settle.service" ];
    unitConfig.ConditionPathExists = "/dev/nvidia0";
    serviceConfig = {
      ExecStart = "${helper}/bin/surface-nvidia-power";
      Restart = "on-failure";
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
        "/dev/nvidiactl rw"
        "/dev/nvidia0 rw"
      ];
    };
  };
}
