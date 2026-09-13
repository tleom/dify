# Workbench Office 沙箱

此目录保存用户沙箱的 Office、PDF、图片、数据分析和浏览器运行环境。基础镜像固定到 digest，Python 和 Node 依赖分别使用 `requirements.lock.txt` 和 `package-lock.json` 安装。

## 构建和验证

```sh
docker build -t workbench-sandbox:office ./workbench/sandbox-office
docker run --rm --network none --shm-size 1g \
  --entrypoint /opt/office/python/bin/python \
  workbench-sandbox:office /opt/office/smoke.py /tmp/office-smoke
```

当前镜像安装 Google Chrome 的 AMD64 软件包，构建平台为 `linux/amd64`。系统软件安装时会获取软件源当前版本；实际版本记录在镜像的 `/opt/office/system-packages.txt` 和 `/opt/office/chrome-version.txt`。自备字体按 `fonts/README.md` 单独提供。

将沙箱管理器的 `WORKBENCH_SANDBOX_IMAGE` 配置为构建后的镜像。运行中的用户容器继续完成当前任务；停止的旧容器在下一次启动时保留为备份，并复用该用户已有的 home、files、env 数据卷创建新容器。

`RUNTIME.md` 是供沙箱内任务读取的工具说明。`office.py` 和 `uno_worker.py` 提供格式转换、预览、公式重算和接受修订；每次 LibreOffice 操作使用独立 profile，输出文件不得覆盖源文件或已有文件。

沙箱管理器和文件操作的回归检查位于 `../tests/`，应在 Linux 环境运行：

```sh
python -m pytest workbench/tests/test_file_ops.py \
  workbench/tests/test_file_directories.py \
  workbench/tests/test_manager_migration.py
```
