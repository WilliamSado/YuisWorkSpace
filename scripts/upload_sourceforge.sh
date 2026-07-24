#!/usr/bin/env bash
set -euo pipefail

device="${1:?device is required}"
artifact_pattern="${2:?artifact glob is required}"
sf_user="${SOURCEFORGE_USER:?SOURCEFORGE_USER is not configured}"
sf_project="${SOURCEFORGE_PROJECT:-yuis-release}"
sf_host="${SOURCEFORGE_HOST:-frs.sourceforge.net}"
release_family="${3:-AviumUI}"
release_date="$(date +%Y%m%d)"
if [[ ! "$device" =~ ^[A-Za-z0-9._+-]+$ || ! "$release_family" =~ ^[A-Za-z0-9._+-]+$ ]]; then
  printf 'Invalid release family or device name\n' >&2
  exit 2
fi
remote_dir="/home/frs/project/${sf_project}/${release_family}/${device}/${release_date}"

shopt -s nullglob
# The pattern is supplied by the trusted device configuration and intentionally
# expanded here so the newest completed ROM is selected.
artifacts=( $artifact_pattern )
shopt -u nullglob
if (( ${#artifacts[@]} == 0 )); then
  printf 'No build artifact matches %s\n' "$artifact_pattern" >&2
  exit 2
fi

artifact="$(ls -1t -- "${artifacts[@]}" | head -n 1)"
checksum="${artifact}.sha256"
sha256sum "${artifact}" >"${checksum}"

printf 'Uploading %s to %s@%s:%s/\n' "$artifact" "$sf_user" "$sf_host" "$remote_dir"
sftp -q -b - \
  -o BatchMode=yes \
  -o ConnectTimeout=30 \
  -o 'ProxyCommand=nc -X connect -x 127.0.0.1:7890 %h %p' \
  "${sf_user}@${sf_host}" <<EOF
-mkdir /home/frs/project/${sf_project}/${release_family}
-mkdir /home/frs/project/${sf_project}/${release_family}/${device}
-mkdir ${remote_dir}
EOF

rsync -avP --partial \
  -e "$(dirname "$0")/sourceforge_ssh.sh" \
  "${artifact}" "${checksum}" \
  "${sf_user}@${sf_host}:${remote_dir}/"
printf 'SourceForge upload completed: %s/%s/%s/%s\n' \
  "$release_family" "$device" "$release_date" "$(basename "$artifact")"
