#!/usr/bin/env bash
set -euo pipefail

device="${1:?device is required}"
dist_dir="${2:?dist directory is required}"
release_family="${3:-LineageOS}"
release_type="${4:-weekly}"
flavor="${5:-vanilla}"
keys_dir="${ANDROID_SIGNING_KEYS_DIR:-/home/ubuntu/.signing/lineage-23.2}"
export TMPDIR="${ANDROID_SIGNING_TMPDIR:-/home/ubuntu/aosp/.tmp/signing}"
install -d -m 700 "$TMPDIR"

available_kb="$(df -Pk "$TMPDIR" | awk 'NR == 2 {print $4}')"
if (( available_kb < 25 * 1024 * 1024 )); then
  printf 'Signing temp directory needs at least 25 GiB free: %s\n' "$TMPDIR" >&2
  exit 2
fi

if [[ ! "$device" =~ ^[A-Za-z0-9._+-]+$ || ! "$release_family" =~ ^[A-Za-z0-9._+-]+$ ]]; then
  printf 'Invalid release family or device name\n' >&2
  exit 2
fi
if [[ ! "$release_type" =~ ^(nightly|weekly|monthly)$ ]]; then
  printf 'Invalid release type: %s (expected nightly, weekly, or monthly)\n' "$release_type" >&2
  exit 2
fi
if [[ ! "$flavor" =~ ^(gms|vanilla)$ ]]; then
  printf 'Invalid flavor: %s (expected gms or vanilla)\n' "$flavor" >&2
  exit 2
fi
flavor_upper="${flavor^^}"

model_file="$dist_dir/LINEAGE_CUSTOM_MODEL"
[[ -s "$model_file" ]] || {
  printf 'Missing LINEAGE_CUSTOM_MODEL metadata: %s\n' "$model_file" >&2
  exit 2
}
custom_model="$(tr -d '\r\n' < "$model_file")"
custom_model="${custom_model// /_}"
if [[ ! "$custom_model" =~ ^[A-Za-z0-9._+-]+$ ]]; then
  printf 'Invalid LINEAGE_CUSTOM_MODEL value: %s\n' "$custom_model" >&2
  exit 2
fi

for key in releasekey platform shared media networkstack sdk_sandbox bluetooth nfc; do
  [[ -r "$keys_dir/$key.pk8" && -r "$keys_dir/$key.x509.pem" ]] || {
    printf 'Missing signing key pair: %s\n' "$key" >&2
    exit 2
  }
done

signer="out/host/linux-x86/bin/sign_target_files_apks"
ota_tool="out/host/linux-x86/bin/ota_from_target_files"
[[ -x "$signer" && -x "$ota_tool" ]] || {
  printf 'Signing host tools are missing; m dist must finish first\n' >&2
  exit 2
}

mapfile -t target_files < <(
  find "$dist_dir" -maxdepth 1 -type f -name '*target_files*.zip' ! -name '*signed*' \
    -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-
)
(( ${#target_files[@]} > 0 )) || {
  printf 'No unsigned target_files archive found in %s\n' "$dist_dir" >&2
  exit 2
}

unsigned="${target_files[0]}"
release_date="$(date +%Y%m%d)"
signed_target_files="$dist_dir/${release_family}-${custom_model}-${flavor_upper}-${release_date}-${release_type}-signed-target_files.zip"
signed_ota="$dist_dir/${release_family}-${custom_model}-${flavor_upper}-${release_date}-${release_type}-signed.zip"

printf '[sign] Input target_files: %s\n' "$unsigned"
printf '[sign] Model: %s · flavor: %s · release type: %s\n' "$custom_model" "$flavor_upper" "$release_type"
printf '[sign] Temporary directory: %s\n' "$TMPDIR"
"$signer" -o -d "$keys_dir" \
  -k "build/make/target/product/security/nfc=$keys_dir/nfc" \
  "$unsigned" "$signed_target_files"

printf '[sign] Generating OTA: %s\n' "$signed_ota"
"$ota_tool" -k "$keys_dir/releasekey" "$signed_target_files" "$signed_ota"
sha256sum "$signed_target_files" "$signed_ota"
printf '[sign] Completed: %s\n' "$signed_ota"
