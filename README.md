# Model Work Trials

在自己的项目里试用一个新模型：交给它一个真实岗位的小任务，保存它看到的材料、实际交付和你对结果的判断，再决定要不要让它接下一单。工具支持手动使用已有订阅，也支持文本 Chat Completions API。需要 Python 3.10 或更高版本，使用标准库。

## 先看一个结果

在仓库根目录运行下面的命令。它用随附的演示回答和人工评分生成报告，整个过程离线。

```sh
python3 trials.py demo runs/demo.sqlite
```

示例里，简报声称新排队方案使总体等待时间缩短了 19%。按表重算，19% 是对的；分线路计算，两条线路各自的等待时间却都上升了。这个任务同时看审核者能否发现归因问题、能否保留正确数字，以及能否交回一段可以使用的文字。演示答案用于熟悉判卷；选模型时请换一份仍未给它看过答案的项目材料。

## 先选岗位

| 你准备交给模型的工作 | 随附试题 | 这一轮能观察什么 |
|---|---|---|
| 审核者 | [客流试点简报](examples/reviewer/task.md) | 查逻辑遗漏、核算事实、报告误报，指出具体改法。 |
| 主力负责人 | [修订客流日报](examples/lead/task.md) | 第一轮给目标、交付物与初始数据；收到可用交付后，在同一会话给出第二批数据和新增约束。分别记录计划、可执行产物、修订和回归情况，以及人在哪一步接管。 |
| 专项执行者 | [值班摘要页](examples/specialist/task.md) | 从成品分别评文字、视觉和交互。请实际打开 HTML，在桌面和窄屏完成表内的查找任务。 |

两轮脚本任务只能观察一次变更下的推进。长期推进和目标延伸咨询，要留在真实项目里判断：决策前记下候选路线、建议依据、最可能改变建议的证据与停止条件；到下一个决策点再核对是否更新了判断、节省了哪一步工作。暂时无法检验的能力记为 `unobserved`。

## 用自己的订阅做一遍

以审核任务为例。下面的命令会新建一次本地记录，冻结题面、各轮资料、评判条件和判分准则。为每次尝试使用新的文件名。

```sh
python3 trials.py start examples/reviewer runs/review-a.sqlite \
  --channel subscription --model '填写实际型号' --client '网页或本机 CLI 及版本' --effort high
python3 trials.py prompt runs/review-a.sqlite > runs/question.txt
```

在一个新会话里只发送 `runs/question.txt`。评分依据、演示答案和未到轮次的反馈留给评者。若使用能查本地目录的代理，把当前题面放到单独的空目录中。按所选工具的正常入口使用，保存它返回的原文为 `runs/answer.txt`，然后记录交付并生成评分表。

```sh
python3 trials.py record runs/review-a.sqlite --stage 1 --status complete --file runs/answer.txt
python3 trials.py rubric runs/review-a.sqlite
python3 trials.py rubric runs/review-a.sqlite --template > runs/grade.json
```

订阅路径记录由操作者提交的答复、附件和回执；网页端的实际模型路由、请求数、用量与工具访问仅在原客户端给出可核验记录时可确认。

多轮任务在每次 `complete` 后重新执行 `prompt`，将下一条消息发到同一会话，再以对应的 `--stage` 记录。没有答复用 `--status failed --note '原因'`；保留了可核对的文字但交付中断，用 `--status partial --file ... --note '原因'`。两者都会结束本次尝试。原始输出可以通过 `--artifact 文件` 附上，可重复指定；`--receipt 文件.json` 可附上客户端终态、耗时和用量等来源信息。

在 `grade.json` 中，每项 `rating` 可取 `met`、`partial`、`missed` 或 `unobserved`。在 `evidence` 写清引用了哪一行、做过什么操作、观察到什么错误。填写派工决定及理由；作者实际核验和返工的分钟数可填入 `human_minutes`，未计时保留 null。整份[示例评分](examples/reviewer/demo-grade.json)可以参照。

```sh
python3 trials.py grade runs/review-a.sqlite runs/grade.json
python3 trials.py report runs/review-a.sqlite > runs/review-a.md
```

派工决定可以是 `try_on_new_work`（换新题再试）、`use_with_review`（在人工核对下使用）、`hold`（暂缓）或 `insufficient_evidence`（证据不足）。考察两个候选时，在同一冻结题目下分别建记录，再运行 `python3 trials.py compare runs/review-a.sqlite runs/review-b.sqlite`。输出并列的逐项结论，同时保留模型、入口、档位和交付状态。相同题面上的不同工具权限也会影响表现，须在 case 的 conditions 或各次回执中交代。

## 使用自己的 API

命令适用于接受 Bearer key、`model`、`messages` 并返回 `choices[0].message.content` 的文本 Chat Completions 端点。在终端环境中设置 `TRIAL_API_KEY`、`TRIAL_MODEL` 和 `TRIAL_ENDPOINT`，后者应是你所选提供方完整的 HTTPS 请求地址。客户端使用当前环境的 HTTPS 代理设置。每次显式调用 `api` 只发当前一轮，可能按提供方价格计费。不同提供方的原生 Messages、Responses 或 Gemini generateContent 协议可以通过其现有客户端作答，再走上面的手动记录流程。

```sh
python3 trials.py start examples/lead runs/lead-api.sqlite \
  --channel api --model "$TRIAL_MODEL" --client 'Chat Completions'
python3 trials.py api runs/lead-api.sqlite --stage 1 --endpoint "$TRIAL_ENDPOINT"
python3 trials.py prompt runs/lead-api.sqlite
python3 trials.py api runs/lead-api.sqlite --stage 2 --endpoint "$TRIAL_ENDPOINT"
```

第二次调用会带上已保存的第一轮题目和真实回答。文本 API 采用整份题面直接提供材料；订阅 CLI 若采用自主检索，应单列条件。两个入口都能交回代码或 HTML 文本，实际运行、浏览检查与人工介入需用操作记录另证。仅当端点支持 `reasoning_effort` 时才为 API 指定 `--effort`。用量与返回的模型名按提供方回执保存。[请求与响应结构示例](https://api-docs.deepseek.com/api/create-chat-completion/)

工具会在发送前占用本轮槽位。重复调用同一轮无法再发请求；网络故障或非正常终止都会保留。若本地客户端崩溃且记录停在 `sending`，先确认旧进程已结束，再用 `interrupt RUN --stage N --note '实际情况'` 标成失败。服务端是否已计费以提供方记录为准。

## 换成自己的项目

1. 从一次真实交付里挑一个会改变派工决定的问题。优先选能追到当时材料、原始交付和后来核验的例子，保留“没有问题”和“只需改说明”这类结论。
2. 复制与岗位相近的 `examples/` 子目录到忽略版本管理的 `private/`；改写根目录的 `case.json`。其中 `stages` 按顺序列出每轮可提供给模型的 UTF-8 文本文件，`rubric` 是评者使用的判据。截图、代码或交互成品可通过 `record --artifact` 保留，`export RUN --stage N 文件名 目标路径` 可取回原始字节。
3. 在看回答前写好足以改变决定的错误、可接受的不同解法，以及什么情况应记“无法观察”。用 `conditions` 写明可用工具、时间、允许几轮和是否需要实跑。冻结后改题会产生不同指纹。
4. 同题回放用来发现特长与回退。若它在有判据的任务上解决了现有主力的遗漏，或明显减少作者返工，再把一份新的未见过的交付交给它。公开示例及其答案均可被阅读，不能当作保密留出集。

本地记录含完整题面、各轮答复、附件、回执和评分。公开模板与本地 `runs/`、`private/` 分开。归档旧答复用 `start --channel replay`，可在 `--receipt` 里标清原始输入与输出哈希，以及哪些能力没有观察到。

开发者可运行 `python3 -m unittest -v` 验证本地流程。API 测试使用模拟响应，覆盖分轮发送、空答复、截断、超时及重复提交。
