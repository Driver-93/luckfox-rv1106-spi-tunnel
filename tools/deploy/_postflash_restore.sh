#!/bin/bash
# 刷机后一键恢复 — 在 PC (Git Bash) 上运行: bash _postflash_restore.sh <板子IP>
set -e
IP="${1:-192.168.3.86}"
KEY="id_ed25519"
SSH="ssh -i $KEY -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@$IP"
SCP="scp -i $KEY -o ConnectTimeout=10 -o StrictHostKeyChecking=no"
KIT=/tmp/restore_kit
cd "$(dirname "$0")"

echo "== 1. 传包 =="
$SCP /tmp/restore_kit.tar root@$IP:/tmp/
[ -f /tmp/mediamtx_plain.tar ] || gunzip -k -c mediamtx_armv7.tar.gz > /tmp/mediamtx_plain.tar
$SCP /tmp/mediamtx_plain.tar root@$IP:/tmp/

echo "== 2. 解包 + 落位 =="
$SSH "tar -xf /tmp/restore_kit.tar -C /tmp && \
  mkdir -p /userdata/car /userdata/hwcfg /root/mediamtx /usr/lib && \
  cp -f /tmp/restore_kit/car/* /userdata/car/ && \
  cp -f /tmp/restore_kit/init/S21spitun /tmp/restore_kit/init/S22spinet /tmp/restore_kit/init/S23web /tmp/restore_kit/init/S24spinet_wd /tmp/restore_kit/init/S25rkipc /tmp/restore_kit/init/S26mediamtx /etc/init.d/ && \
  chmod +x /etc/init.d/S21spitun /etc/init.d/S22spinet /etc/init.d/S23web /etc/init.d/S24spinet_wd /etc/init.d/S25rkipc /etc/init.d/S26mediamtx /userdata/car/cam_up.sh && \
  cd /root/mediamtx && tar -xf /tmp/mediamtx_plain.tar mediamtx && chmod +x mediamtx && \
  cp -f /tmp/restore_kit/mtx/mediamtx.yml /root/mediamtx/mediamtx.yml && \
  cp -f /tmp/restore_kit/npu/object_detection_pfp.data /tmp/restore_kit/npu/object_detection_pfp_896x512.data /usr/lib/ && \
  cp -f /tmp/restore_kit/npu/rkipc_pet /oem/usr/bin/rkipc && chmod +x /oem/usr/bin/rkipc && \
  echo KIT_OK"

echo "== 3. rkipc 配置 (H264 + GOP12 + NPU on) =="
$SSH "cp -f /oem/usr/share/rkipc-300w.ini /userdata/rkipc.ini 2>/dev/null; \
  for f in /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini; do \
    sed -i 's/output_data_type = H.265/output_data_type = H.264/' \$f; \
    sed -i 's/^gop = .*/gop = 12/' \$f; \
    sed -i 's/^enable_npu.*/enable_npu = 1/' \$f; \
    sed -i 's/^npu_fps.*/npu_fps = 15/' \$f; \
  done; grep -E 'output_data_type|^gop|enable_npu|npu_fps' /userdata/rkipc.ini | head -6"

echo "== 3b. sshd 自启 (原厂无 init 脚本) =="
$SSH "chown root:root /root 2>/dev/null; cat > /etc/init.d/S50sshd <<'EOF'
#!/bin/sh
case \$1 in
  start) /usr/sbin/sshd 2>/dev/null ;;
esac
exit 0
EOF
chmod +x /etc/init.d/S50sshd; /usr/sbin/sshd 2>/dev/null; netstat -tln | grep ':22' | head -1"

echo "== 4. paho-mqtt =="
$SSH "tar -xf /tmp/restore_kit/paho.tar -C /usr/lib/python3.11/site-packages 2>/dev/null || tar -xf /tmp/restore_kit/paho.tar -C \$(python3 -c 'import site;print(site.getsitepackages()[0])'); python3 -c 'import paho.mqtt.client; print(\"paho OK\")'"

echo "== 5. 重启生效 =="
$SSH "reboot" 2>&1 | tail -1 || true
echo "板子重启中, 100 秒后跑: bash _postflash_verify.sh $IP"
