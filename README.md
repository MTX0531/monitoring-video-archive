# 门店监控录像本地归档（rtsp-puller）

从本地**大华 NVR 录像机**通过 RTSP 把监控回放录像按 **≤30 分钟/段** 拉取到本机磁盘，自动完成磁盘选址、归档命名、过期清理，并支持 NVR 录像机 FTP 推送的接收与入库。后续 AI 视频稽核 / 上传 Notion 通过钩子接入。

## 仓库结构

```
rtsp-puller/
├── rtsp-puller/
│   ├── nvr_puller.py           # 主程序：RTSP 回放拉流归档
│   ├── config.example.json     # 配置模板（脱敏，复制为 config.json 后填写）
│   ├── install_startup.ps1     # 开机自启安装
│   ├── run_daily.bat           # 每日任务
│   ├── run_forever.bat         # 常驻运行
│   ├── nvrcore/                # 核心库（拉流/下载/存储/状态/FTP 入库）
│   ├── tools/                  # 运维与测试工具（FTP 接收、容量规划、清理、自测）
│   └── docs/多门店部署方案.md    # 多门店部署说明
```

## 功能特性

- **RTSP 回放拉取**：大华专用地址格式，自动探测录像起始、跳过无录像空档（404 秒判）
- **并发提速**：单路固定 1 倍速，支持多路并发回放线性提速（整机实测 15 路上限）
- **自动归档**：按门店名/通道名/时间命名，自动磁盘选址，按日组织
- **留存管理**：按天数 / 容量上限 / 磁盘剩余空间三级清理，永久删除模式
- **FTP 接收**：接收录像机 FTP 推送并转封装入库，支持空闲自动收工
- **容错**：断线重连、卡死检测、录像校验修复（无损重封装 + 转码兜底）

## 快速开始

1. 复制配置模板并填写真实参数：
   ```bash
   cp config.example.json config.json
   # 编辑 config.json：nvr.host / nvr.username / nvr.password / 归档目录等
   ```
2. 单次运行（前台，当日归档）：
   ```bash
   python nvr_puller.py
   ```
3. 常驻运行 / 开机自启：
   ```bash
   run_forever.bat        # 或 install_startup.ps1 注册开机自启
   ```

## 重要说明

- `config.example.json` 中地址与密码为**脱敏占位**，部署时必须替换为真实录像机地址与凭据
- 拉流时段默认限制在门店营业时间（09:30–22:30），避免占用录像机回放通道
- 归档文件命名：`<门店名>_<通道名>_<起>_<止>_device.mp4`
- 实测设备：大华 DH-NVR4216-HDS2（实机参数与避坑点见 `使用须知.md` 与 `docs/项目流程.md`）
