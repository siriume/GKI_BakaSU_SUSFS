# 源码 Release 归档

## 分组和内容

每个 Android KMI / 内核系列使用一个预发布 Release，例如 `source-android13-5.15`、`source-android12-5.10`。这些 Release 不设为 latest，不影响正式内核发布。

归档在任何 BakaSU、SUSFS、功能补丁和 defconfig 修改前生成。保存 manifest 中全部项目的工作树、浅克隆 Git 元数据及原配预编译工具链，保留符号链接和执行权限。额外下载的 LineageOS GCC 单独保存在 `source-toolchains`，以完整 commit 命名，不重复装进各个源码包。BakaSU/SUSFS 仍在解压后按构建选项拉取、集成。

## 命名和完整性

固定版本索引示例：

```text
source-android13-5.15
  android13-5.15.123-2023-11-r9-v1.json
  android13-5.15.123-2023-11-r9-v1-<源码摘要>-<上传标识>.tar.zst.part0000
```

LTS 索引包含完整源码摘要；其压缩分卷文件名另含实际内核子版本，例如 `android13-5.15.220-lts-v1-...tar.zst.part0000`。JSON 保存实际内核版本、锁定 manifest、额外 GCC commit、源码摘要，以及各分卷的文件名、字节数和 SHA256。

每卷最大 1900 MiB。恢复时按 JSON 中的顺序拼接，校验每卷大小和 SHA256，解压后再逐个核对 Git HEAD。完整源码核对通过后才替换构建目录。固定版本还会检查 Makefile 子版本是否与输入一致。下载、认证和校验失败会明确报错，不会伪装成未命中然后全量拉取。

全部分卷上传成功后才上传 JSON 完成索引。并发上传使用不同的分卷文件名，避免覆盖另一份索引正在引用的内容。中断上传留下的未引用分卷不会被读取。

手工解压时，请按 JSON 的 `parts` 顺序合并源码分卷，再解压到源码根目录；额外 GCC 分卷需另行合并并解压到该目录的 `gcc/`。构建流程会自动完成这些操作。

## 更新规则

- 固定月份/revision：索引存在时直接复用，连首次归档锁定的辅助项目和 GCC 提交也一并固定，不再请求 Google 的 manifest。
- LTS：每次先解析当前 manifest 的全部项目和额外 GCC commit；只有完整提交组合发生变化才生成新的源码归档。历史包保留，文件内容不随 `lts` 名称改变。
- `cache_epoch`：构建与源码更新流程均默认为 `v1`。要重新解析并生成固定版本归档，可两边一起改成 `v2` 等新值。
- 内核构建：归档缺失时自动同步、验证、上传，然后继续打补丁和编译。
- 源码更新流程：归档存在时只检查完成索引，缺失时同步、验证和上传，不编译内核。

## 自动和手动运行

在 Actions 选择 **更新源码 Release**，可指定 `source_group`（如 `android13-5.15`）、`targets`（`configured` 或 `lts`）和 `cache_epoch`。`configured` 使用现有 JSON 与内核构建相同的子版本去重规则，不会枚举 JSON 外的上游分支，也不会修改版本选择。

每天 UTC 19:00（北京时间次日 03:00）自动检查全部配置；**更新 GKI 版本数据** 工作流在 main 成功完成后也会触发检查。首次检查会补齐所选配置中全部缺失归档，后续固定版本只读取索引，LTS 按提交变化更新。矩阵最多同时运行两个归档任务。

所有源码 Release 均写入运行工作流的仓库，使用内置 `GITHUB_TOKEN` 的 `contents: write` 权限，无需额外 secret。分叉仓库会维护自己的源码 Release。

原先的 `source-v1-*` Actions cache 不再读写，已有条目可以手动删除或等待过期。ccache/Bazel、ThinLTO 及小型构建工具缓存沿用现有配置。
