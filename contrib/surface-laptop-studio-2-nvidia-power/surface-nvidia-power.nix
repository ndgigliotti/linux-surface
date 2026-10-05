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
    # Retry late device-node creation instead of permanently skipping startup.
    unitConfig.StartLimitIntervalSec = 0;
    serviceConfig = {
      # Stat only: no device open or GPU wake while waiting for the nodes.
      ExecStartPre = [
        "${pkgs.coreutils}/bin/test -c /dev/nvidiactl"
        "${pkgs.coreutils}/bin/test -c /dev/nvidia0"
      ];
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
