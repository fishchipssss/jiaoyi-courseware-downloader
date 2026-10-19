# 交医课件下载器

面向上海交通大学医学院教学课表的 Windows 课件下载工具。选择一个日期，读取学校课表提供的教学周和课程链接，将这一周的 PPT、PPTX、PDF 下载到本地。

这是个人开发的工具，与学校官方没有隶属关系。仓库只提供程序，不包含账号、密码、登录会话或任何课程文件；使用者需拥有对应课程的访问权限。

## 功能

- 中文桌面界面、日期选择、保存位置选择和任务日志。
- 从学校首页登录后读取课表 API，以网页教学周数据为准，支持周日调课。
- 自动建立 `Week 01`、`Week 02` 等周目录；无法识别教学周时使用日期范围。
- 使用电脑上的 Chrome，并为下载器单独保存登录状态。
- 大文件分块下载、进度输出、失败后继续、临时失败重试。
- PPT/PPTX/PDF 结构检查；未通过检查的文件不会标记成功。
- 保留下载记录，再次运行时跳过已记录且有效的文件。
- 失败清单包含课程日期、课程名称和具体原因。

## 安装

需要 Windows 10/11、Python 3.12 或更新版本，以及已经安装的 Google Chrome。

在项目文件夹打开 PowerShell，运行：

```powershell
python -m pip install -r requirements.txt
```

默认使用系统 Chrome，无需额外安装 Chromium。若要使用独立 Chromium：

```powershell
python -m playwright install chromium
python download_ppt_by_week.py --browser chromium --date 14/10/2026
```

## 配置账号

将 `credentials.example.json` 复制为同目录的 `credentials.local.json`：

```powershell
Copy-Item credentials.example.json credentials.local.json
notepad credentials.local.json
```

在本地填写以下字段：

| 字段 | 用途 |
| --- | --- |
| `school_username` | 学校登录账号，用于自动填写；也可以留空并在浏览器手动登录。 |
| `school_password` | 学校登录密码；验证码和额外验证需要手动完成。 |
| `file_username` | 课件服务器的二次认证账号，与首次学校登录分开。 |
| `file_password` | 课件服务器的二次认证密码；需要二次认证时应提前配置。 |
| `output_root` | 保存根目录；留空时使用用户主目录下的 `Documents/交医课件`。 |

Windows 路径可写成 `D:/课程资料`，避免 JSON 中反斜杠转义的问题。本地配置以明文保存，仅存放在自己的电脑上；`.gitignore` 已排除它，提交代码前仍应检查暂存文件。

也支持 `SCHOOL_USERNAME`、`SCHOOL_PASSWORD`、`CURRICULUM_USERNAME`、`CURRICULUM_PASSWORD` 环境变量，优先级高于本地账号配置。不要将真实密码写进公开源码、命令示例或问题反馈。

## 使用

1. 双击 `【双击这里启动】课件按周下载器.bat`，或运行 `python ppt_downloader_app.py`。
2. 选择目标日期与保存根目录，点击开始。
3. 如果浏览器要求登录，先在学校首页完成登录，保留该浏览器窗口，再回 App 点击“登录完成，继续”。已有有效会话时会直接读取课表。
4. 查看任务日志。二次认证只在课件请求收到认证挑战后提交，不会给首次登录页面设置全局密码。
5. 在周文件夹内查看 `下载结果.txt` 和课件。`download-results.json` 用于后续识别已下载文件，请一起保留。

命令行示例：

```powershell
python download_ppt_by_week.py --date 14/10/2026 --output "D:/课程资料"
```

支持 `14/10/2026`、`2026-10-14` 等日期格式。学年和学期通常按日期推断，特殊情况可以通过 `--school-year`、`--semester`、`--grade` 调整。完整参数见 `python download_ppt_by_week.py --help`。

停止或关闭 App 时，会结束本次下载器启动的浏览器。不要同时启动多份下载器。

## 常见问题

**提示连接 `127.0.0.1` 代理失败**：检查代理软件实际监听端口、Windows 系统代理，以及 `ALL_PROXY` 等环境变量是否一致。Windows 版会在启动下载任务时重新读取用户保存的 `ALL_PROXY`，但不会自动修改代理端口。

**HTTP 401**：检查本地课件二次认证配置；首次学校登录和文件服务器可能使用不同凭据。

**HTTP 404**：网页提供的链接可能已失效，需要确认老师是否更新了附件。

**PPTX 校验失败**：保存下来的内容不完整或格式异常。可与手动下载结果对比，不能仅凭 HTTP 200 或文件存在认定下载成功。

**显示部分失败**：其他课件会继续下载，最终失败数大于零时返回代码为 1。查看 `下载结果.txt`，重试时成功文件会跳过。

学校页面、登录方式和接口可能发生变化；当前实现针对 `jwstu.shsmu.edu.cn` 与其 WebVPN 流程，不能保证其他学校或未来页面版本直接适用。

## 开发与验证

```powershell
python -m unittest discover -s tools -p "test_*.py" -v
python tools/verify_tile_menu.py --full
```

测试使用本地模拟服务器、临时目录和独立浏览器资料，不需要真实学校账号。界面检查需要图形桌面。

核心模块：`download_ppt_by_week.py` 读取课表与筛选日期，`course_downloads.py` 下载和校验，`downloader_runtime.py` 管理进程与代理环境，`ppt_downloader_app.py` / `course_menu_ui.py` 提供桌面界面。

## 许可

程序源码采用 MIT 许可证。界面中的 AI 生成插画和程序生成音效见 `assets/README.md`；学校课件不属于本仓库，不随程序分发。
