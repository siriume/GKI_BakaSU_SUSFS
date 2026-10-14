#!/usr/bin/env bash
# Shared by kernel builds and the independent source archive updater.
set -euo pipefail

root="$GITHUB_WORKSPACE/$SOURCE_ANDROID-$SOURCE_KERNEL-$SOURCE_SUBLEVEL"
locked="$RUNNER_TEMP/source-locked.xml"
helper=(python3 "$GITHUB_WORKSPACE/scripts/source_release.py")
args=(--android "$SOURCE_ANDROID" --kernel "$SOURCE_KERNEL" --sublevel "$SOURCE_SUBLEVEL"
      --patch "$SOURCE_PATCH" --revision "$SOURCE_REVISION" --epoch "$SOURCE_EPOCH"
      --root "$root" --locked "$locked")

retry() {
  local attempt=1
  until "$@"; do
    if [ "$attempt" -ge 4 ]; then
      echo "::error::命令重试后仍失败: $*" >&2
      return 1
    fi
    sleep $((15 * attempt))
    attempt=$((attempt + 1))
  done
}

verify_sublevel() {
  if [ "$SOURCE_PATCH" != lts ]; then
    local actual
    actual=$(awk '/^SUBLEVEL = / {print $3; exit}' "$root/common/Makefile")
    if [ "$actual" != "$SOURCE_SUBLEVEL" ]; then
      echo "::error::源码子版本号为 $actual，预期为 $SOURCE_SUBLEVEL" >&2
      return 1
    fi
  fi
}

# Miss is exit 3; authentication, download and integrity failures must not be
# mistaken for a missing archive and cause an unnecessary full source sync.
restore() {
  local status=0
  local mode=()
  if [ "${SOURCE_UPDATE_ONLY:-false}" = true ]; then
    mode+=(--metadata-only)
  fi
  "${helper[@]}" restore "${args[@]}" "${mode[@]}" "$@" || status=$?
  if [ "$status" -eq 0 ]; then
    if [ "${SOURCE_UPDATE_ONLY:-false}" != true ]; then
      verify_sublevel || exit 1
    fi
    echo "### 源码：Release 已就绪 ($SOURCE_ANDROID / $SOURCE_KERNEL / $SOURCE_PATCH)" >> "$GITHUB_STEP_SUMMARY"
    return 0
  fi
  if [ "$status" -ne 3 ]; then
    exit "$status"
  fi
  return 1
}

# A fixed monthly target reuses its first complete lock, including the original
# toolchain commits. It no longer needs Google's manifest service on a hit.
if [ "$SOURCE_PATCH" != lts ] && restore; then
  exit 0
fi

mkdir -p "$root" "$RUNNER_TEMP/git-repo"
repo="$RUNNER_TEMP/git-repo/repo"
retry curl -fLSs https://storage.googleapis.com/git-repo-downloads/repo -o "$repo"
chmod 0755 "$repo"
cd "$root"
formatted="$SOURCE_ANDROID-$SOURCE_KERNEL-$SOURCE_PATCH"
echo "初始化 repo，分支: common-$formatted"
retry "$repo" init --depth=1 -u https://android.googlesource.com/kernel/manifest \
  -b "common-$formatted" --repo-rev=v2.16
manifest=.repo/manifests/default.xml
remote_branch=$(retry git ls-remote --heads https://android.googlesource.com/kernel/common "refs/heads/$formatted")
release_tag=""
if [ -n "$SOURCE_REVISION" ] && [ "$SOURCE_PATCH" != lts ]; then
  release_tag="${formatted}_${SOURCE_REVISION}"
  tag_ref=$(retry git ls-remote --tags --refs https://android.googlesource.com/kernel/common "refs/tags/$release_tag")
  if [ -z "$tag_ref" ]; then
    echo "::error::未找到 JSON 指定的发布 tag: $release_tag" >&2
    exit 1
  fi
elif [ -z "$remote_branch" ] && [ -n "$(retry git ls-remote --heads https://android.googlesource.com/kernel/common "refs/heads/deprecated/$formatted")" ]; then
  sed -i "s|\"$formatted\"|\"deprecated/$formatted\"|g" "$manifest"
elif [ -z "$remote_branch" ]; then
  tag_refs=$(retry git ls-remote --tags --refs https://android.googlesource.com/kernel/common "refs/tags/${formatted}_r*")
  if [ -z "$tag_refs" ]; then
    echo "::error::未找到 $formatted 分支或对应的发布 tag" >&2
    exit 1
  fi
  release_tag=$(printf '%s\n' "$tag_refs" | awk '{print $2}' | sed 's|^refs/tags/||' | sort -V | tail -n1)
fi
if [ -n "$release_tag" ]; then
  sed -i "s|revision=\"$formatted\"|revision=\"refs/tags/$release_tag\"|" "$manifest"
  if ! grep -qF "revision=\"refs/tags/$release_tag\"" "$manifest"; then
    echo "::error::未能在 manifest 中设置 kernel/common 的发布 tag" >&2
    exit 1
  fi
fi

"$repo" manifest -o "$RUNNER_TEMP/source-manifest.xml"
gcc_sha=""
if [[ "$SOURCE_KERNEL" == 5.10 || "$SOURCE_KERNEL" == 5.15 ]]; then
  gcc_sha=$(retry git ls-remote https://github.com/LineageOS/android_prebuilts_gcc_linux-x86_aarch64_aarch64-linux-gnu-6.4.1.git HEAD | awk '{print $1}')
  if [[ ! "$gcc_sha" =~ ^[a-fA-F0-9]{40}$ ]]; then
    echo "::error::无法锁定额外 GCC 工具链提交" >&2
    exit 1
  fi
fi
digest=$(python3 "$GITHUB_WORKSPACE/scripts/source_snapshot.py" "$RUNNER_TEMP/source-manifest.xml" \
  "$locked" --extra-revision "$gcc_sha" --sync-manifest "$RUNNER_TEMP/source-sync.xml")
cp "$RUNNER_TEMP/source-sync.xml" "$manifest"

# LTS identity includes every manifest project and the extra GCC revision.
if restore --digest "$digest"; then
  exit 0
fi

"${helper[@]}" restore-gcc "${args[@]}" --gcc-sha "$gcc_sha"
if [ -n "$gcc_sha" ]; then
  if [ ! -d gcc/.git ]; then
    git init gcc
    git -C gcc remote add origin https://github.com/LineageOS/android_prebuilts_gcc_linux-x86_aarch64_aarch64-linux-gnu-6.4.1.git
    retry git -C gcc fetch --depth 1 origin "$gcc_sha"
    git -C gcc checkout --detach FETCH_HEAD
  fi
  if [ "$(git -C gcc rev-parse HEAD)" != "$gcc_sha" ]; then
    echo "::error::共享 GCC 工具链提交不匹配" >&2
    exit 1
  fi
fi

for attempt in 1 2 3; do
  echo "源码同步：第 $attempt/3 次尝试 ($(date -u +%FT%TZ))"
  if "$repo" sync -c "-j$(nproc)" --no-tags --fail-fast --verbose; then
    break
  fi
  if [ "$attempt" -eq 3 ]; then
    exit 1
  fi
  sleep $((attempt * 30))
done
python3 "$GITHUB_WORKSPACE/scripts/source_snapshot.py" "$locked" --verify-root "$root" --extra-revision "$gcc_sha"
verify_sublevel

# The archive is saved before any KernelSU/SUSFS/configuration modifications.
"${helper[@]}" save "${args[@]}" --digest "$digest" --gcc-sha "$gcc_sha" --release-tag "$release_tag"
echo "digest=$digest" >> "$GITHUB_OUTPUT"
echo "hit=false" >> "$GITHUB_OUTPUT"
echo "### 源码：已同步并保存至 Release ($SOURCE_ANDROID / $SOURCE_KERNEL / $SOURCE_PATCH)" >> "$GITHUB_STEP_SUMMARY"
