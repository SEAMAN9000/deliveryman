# user 档的四种形态与程序入口

mode 为 `user` 且报告出现"形态推测·请拍板"、用户想改形态、或脚本报了与 `user.entry` / `user.artifacts` 有关的错误时读本文件。

## 四种形态

| 形态 | 怎么得到 | 副本里有什么 |
|---|---|---|
| 程序 program | 配置里声明 `user.entry` | 入口文件 + 说明文档 + 启动器（`RUN.bat`、`run.sh`） |
| 文档 document | 两者都没声明 | 说明文档即内容，没有可执行入口 |
| 成品 artifact | 只声明 `user.artifacts` | 只有点名的成品文件，默认不带 README、LICENSE、启动器 |
| skill | 两者都没声明，且脚本认出项目是一个 skill | skill 文件夹里除测试外的全部文件，加上项目根的 README、LICENSE；skill 文件夹成为副本的根 |

`artifacts` 是"额外带上"的通道，不是"强制保留"的通道：命中必删组的文件不会因为写进 `artifacts` 而进入副本。

## 形态由谁决定

优先级：配置声明 > 上次拍板的沿用 > 脚本推测。

- **配置声明**：直接采用，不推测。
- **推测**：没有声明时，脚本在"skill""成品""文档"之间推测。先看是不是 skill（见下一节）；不是的话，项目里有 pdf、pptx、docx 等成品文件就推测成品并逐个列出候选，否则推测文档。脚本不猜程序入口；发现 `dist/`、`build/`、`target/` 这类产物目录时只会提示"可能是程序，入口是哪个"，入口路径要用户自己给。
- **沿用**：用户确认过的形态记在 `output/deliver/_state.json` 里，连同一份项目结构指纹。指纹没变就沿用、不再问；项目结构变了就重新推测并在报告里告警。这份记录由脚本在用户确认后写入，不要去改源项目的配置文件。

## 怎样算"项目是一个 skill"

脚本找带 `name` 字段的 `SKILL.md`：先看项目根；根上没有，再看 `.claude/skills/`、`.agents/skills/`、`.opencode/skills/`、`skills/`、`work/`、`src/` 以及项目根的下一层文件夹。

- 正好找到一个：推测为 skill 形态，报告的"形态推测·请拍板"一节会写明是哪个文件夹、会带什么。
- 找到多个：不自动选，报告里告警。请用户在 `deliver.config.yaml` 的 `user.artifacts` 里点名要发的那个 skill 的文件。
- skill 之外还有成品文件：skill 放在 AI 工具的自用位置（`.claude/skills/` 等）时按成品推测；放在其他位置时仍按 skill 推测，并在依据里说明有几个成品文件没带。

skill 形态不带的内容：skill 文件夹里的 `tests/`、`test/`，以及 skill 文件夹之外的全部开发文件（项目根的 README、LICENSE、NOTICE 除外）。skill 文件夹里已有 README 时，用它，不用项目根的。密钥文件等必删内容照常去掉。

给开发者档（dev）不受影响：整个项目照原结构带上，含 skill 的测试。

## 用户想改形态

- "改成发文档" / "改成发成品" / "按 skill 发"：给 `scan.py` 加 `--variant document`、`--variant artifact` 或 `--variant skill` 重扫。
- "这是程序，入口是 X"：请用户在 `deliver.config.yaml` 写 `user.entry: X`，再重扫。

## 脚本会直接拒绝的情况

遇到下列情况脚本以非 0 退出并说明原因，把原因转达给用户即可，不要绕过：

- `user.entry` 或 `user.artifacts` 写了绝对路径、盘符、网络路径或 `..`。
- 入口指向目录。入口必须是单个可启动文件；要带整个产物目录，把入口指向其中的启动文件，其余用 `user.extra_include` 声明。
- 入口文件不存在，且没有 `build_cmd` 可以生成它。
- 程序或文档形态但副本根没有 README。
- 成品形态但 `artifacts` 一个文件都没匹配到。
- 指定了 `--variant skill`，但项目里没有 skill、有多个 skill，或配置里已声明了入口或成品。
- 入口文件命中密钥、版本库等必删组。

## 构建命令

`user.build_cmd` 来自被打包项目自己的配置，由 `package.py` 在源项目根执行（上限 10 分钟），等于在用户电脑上运行项目指定的命令。调用 `package.py` 之前把命令原文给用户看并得到同意；用户已自行构建过就加 `--skip-build`。日志写到副本之外的 `output/deliver/_logs/`，因为日志里可能有本机路径。构建失败时保留已生成的副本目录供排查。
