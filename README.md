# ClashAgent

在 Windows 上通过 MuMu 自动循环刷《部落冲突》国际服资源。村庄使用武神和可用英雄，夜世界使用女巫和可用英雄。

## 安装和启动

在项目根目录执行以下命令。已有 `clash` 环境时可以跳过创建步骤。

~~~powershell
conda create -n clash python=3.12 pip
conda activate clash
python -m pip install -r requirements.txt
python main.py
~~~

之后启动只需激活 `clash` 并运行 `python main.py`。

- **ADB** 在“设备连接”中留空即可复用本机服务。需要启动服务时，优先使用正在运行的 MuMu 自带的 ADB，再查找 `PATH` 和 Android SDK。也可用“浏览…”手动选择。没有可用的 ADB 时，从 [Google Platform-Tools](https://developer.android.com/tools/releases/platform-tools) 下载 Windows 版，解压后选择其中的 `adb.exe`。
- **MaaTouch** 和许可证随仓库提供，用于点击和地图手势。
- **截图**优先使用正在运行的 MuMu 自带的原生截图 DLL。程序会自动定位并用 ADB 画面对照校验。找不到 DLL、校验失败或原生截图不可用时，自动使用 ADB 截图。

## 使用

1. 手动打开 MuMu、启用 ADB，以 16:9 横屏打开游戏。在 Editor 的“设备连接”页连接实例。
2. 进入对应基地，再打开 Editor 的“村庄刷资源”或“夜世界刷资源”页。
3. 点击“读取最大容量”保存容量，配置停止条件后开始刷资源。仓库升级或切换账号后可重新读取。
4. 使用主按钮暂停或继续，点击“停止”结束任务。暂停时游戏计时继续。

- **村庄**需要预设武神。每轮回村后检查资源，任一已勾选且有容量记录的资源达到设定比例即停止。默认比例为 **90%**。
- **夜世界**需要将出战部队和第二阶段援军预设为女巫，并先保存金币最大容量。每阶段分别下兵，女巫出场后自动释放蝙蝠技能。每轮回基地后，金币达到已保存容量的 **90%** 即停止。

脚本会校准地图、试投确认落点，再投放部队和英雄。

## 项目结构

- `src/core/` 负责画面识别、地图校准、下兵策略、任务流程和停止条件。
- `src/infrastructure/` 提供 ADB、MuMu 截图、触控，以及配置、模板和日志的文件读写。
- `src/editor/` 提供桌面界面，把任务流程和设备接口接到一起。
- `resources/` 存放流程配置和识别素材。

本机配置保存在 `data/`。连接配置为 `settings.json`，村庄和夜世界分别使用 `village_farm.json` 与 `builder_farm.json`。旧版村庄配置 `farm.json` 会在新版 Editor 首次读取时迁移。

运行日志和暂停截图保存在 `logs/runs/`。

界面与任务设计参考 [MAA](https://github.com/MaaAssistantArknights/MaaAssistantArknights)。触控组件来自 [MaaTouch](https://github.com/MaaAssistantArknights/MaaTouch)。第三方组件保留各自许可，游戏素材与商标归相应权利人。项目源码许可证尚未指定。
