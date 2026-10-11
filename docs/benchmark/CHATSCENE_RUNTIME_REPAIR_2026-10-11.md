# ChatScene 本机运行修复与 Judge 边界（2026-10-11）

## 结论

ChatScene 开发运行配置已修复并实测。实际 API 解析、检索拼装与 CARLA 编译可运行；一条真实开发题的生成场景随后因不存在的相邻车道引用而未通过 SV，作为方法输出失败保留。另一个明确标记的合成场景通过完整 compile、五次 SV 和 NE，证明执行器能够工作，不能替代真实题目的成功记录。

本次使用用户 `.env` 中的 `DeepSeek-V4.1-Flash` OpenAI 兼容接口，temperature=0.2、API timeout=60秒、SDK max_retries=2。沿用默认检索模式：模型解析文本，sentence-t5-large 检索并拼装已有片段；未启用 `--use_llm`。这是单独的开发 profile，不是原 gpt-4o 基线的质量复现或正式结果。

## 已完成与实际制品

- CARLA 已解压到 `.carla-runtime/carla-0.9.13-server/`，离屏 Vulkan 服务在独立2100端口验证。实际服务端版本为 `0.9.13-2-g0c41f167c-dirty`，客户端为0.9.13；保留其 SafeBench 定制版本差异，未伪称干净官方包。
- 既有 `chatscene/` 环境恢复 Torch 1.13.1+cu117、torchvision 0.14.1+cu117、sentence-transformers 2.2.1、transformers 4.46.3、huggingface-hub 0.25.2、OpenAI 2.2.0 等原记录核心版本；Python为3.8.20。补齐CARLA egg需要的pkg_resources，并把同一基础Python的标准库复制进环境，使隐藏checkout后的解释器仍可运行。完整实际依赖清单位于生成的environment JSON。
- sentence-t5-large 本地副本位于 `chatscene/share/models/sentence-t5-large/`，公开版本 `1b36eb48a1df42a07ffd02234d25abfbf3e3f3cb`。配置绑定其文件，并让隔离进程读取只读环境中的本地副本。
- 最终本机配置在 `.carla-runtime/chatscene-local-20261011-final/`，包含method/platform/controller、两份环境清单、development roster及preflight。旧配置未覆盖。执行类预检没有blocker；正式状态仍为draft。模型文件已有本地登记，原preflight的正式模型资源准入项仍保守保留，不代表正式资源闭合已经实现。
- `.env` 已忽略入库；凭据只在内存传入，未写入配置或命令行，诊断输出脱敏。外部进程只收到query文本，没有oracle或评测答案。生成工作目录位于仓库外私有 `/tmp/chatscene-isolated-generation-*`，避免被原隔离规则拒绝；路径记录在诊断结果里。

代码修复包括：不再覆盖API环境变量；显式选择OpenAI兼容后端，原auto选择和未指定时的SDK超时/重试默认保留；允许使用原检索模型的本地路径；把选定CARLA端口写入原始Scenic产物；为自包含Python环境设置沙箱内PYTHONHOME；修正ego尺寸标准化末尾逗号；为Python定义的固定控制器补齐Scenic编译器通常生成的空guard接口。控制器公式、参数、预算、原场景语义和Judge安全检查均未放宽。

## 验证与失败记录

开发题 `BSG_DEV_v0_2_S01_precise` 经真实接口和隔离适配器生成成功，产物可连接CARLA并编译。首次诊断因缺pkg_resources失败，原记录在 `.carla-runtime/chatscene-real-query-20261011/`；补依赖后的生成记录在 `chatscene-real-query-20261011-v2/`。原始生成字节保留在该记录指向的工作目录。

实际生成产物的native诊断在 `.carla-runtime/chatscene-runtime-real-20261011-v3/`：compile通过，五次SV尝试均因 `AttributeError: 'NoneType' object has no attribute 'lane'` 失败，NE按原规则跳过。未补车道、改事件或过滤失败。

已知合成场景的完整记录在 `.carla-runtime/chatscene-runtime-fixture-20261011-v2/`：compile通过，SV固定种子0–4全部通过；NE使用最低通过种子0，固定控制器执行300步、30.0模拟秒。步数和时间仅描述这一合成样例的运行预算，不是生成质量指标或跨方法比较。原控制器缺guard导致的失败记录也保留。

最终定向回归13项通过（2.697秒），覆盖凭据保护、显式API后端与原默认行为、端口持久化、ego-only语义守卫、真实Scenic行为启动、隔离环境以及preflight脱敏/正式门槛。独立reviewer循环通过。旧文件和中间失败目录保留，没有将合成证据改名为真实方法结果。

所有本轮运行均为development diagnostic，`generation_chain_verified=false`；没有形成正式五次重复网格、human gold、Judge校准或正式分数。完整Python运行时闭合校验等正式门槛仍未完成，代码现有hard gate保持不变。

## 重启与使用

本轮验证后已关闭临时2100端口CARLA服务以释放GPU；未停止其他服务。继续运行时，先启动服务，再为当前文件和环境生成一个全新目录的配置。原服务端目录或依赖发生变化后须重新登记，不能修改旧摘要掩盖变化。

```bash
.carla-runtime/carla-0.9.13-server/CarlaUE4.sh -RenderOffScreen -vulkan -nosound -carla-rpc-port=2100 -carla-streaming-port=2101

chatscene/bin/python-frozen scripts/prepare_chatscene_runtime.py \
  --server-root .carla-runtime/carla-0.9.13-server \
  --api-env .env --output .carla-runtime/chatscene-local-new
```

单题真实API诊断会消耗模型请求；输出目录必须不存在。它不计入正式重复次数：

```bash
chatscene/bin/python-frozen scripts/check_chatscene_generation.py \
  --config .carla-runtime/chatscene-local-new/method.json \
  --api-env .env --output .carla-runtime/generation-diagnostic-new

chatscene/bin/python-frozen scripts/check_chatscene_runtime.py \
  --config-dir .carla-runtime/chatscene-local-new \
  --generation-result .carla-runtime/generation-diagnostic-new/result.json \
  --output .carla-runtime/native-diagnostic-new
```

`--help-only` 可只验证真实隔离入口导入，不调用模型；没有生成产物是该模式的预期结果。正式运行仍须使用原generation/runtime命令及通过全部freeze准入，不能把上述诊断结果直接当作benchmark重复记录。

## 不升级宿主内核时的 Judge 方案

本机Linux仍为5.15.0-139-generic，MFD_EXEC/MFD_NOEXEC_SEAL能力探测均返回EINVAL。限制来自现有Judge执行器要求的不可变可执行内存文件及执行权限封闭，不是模型API不可用；本轮没有修改或删除这些检查。

推荐把**Judge执行、结果校验及依赖它的正式评分**放在另一台通过能力探测的机器，或有独立兼容内核的虚拟机，当前宿主内核保持不变。现有Python runner的 `validate_judge_run_bundle → validate_judge_request_bundle → _frozen_judge_context → _load_frozen_judge_runner_material` 在校验阶段也会执行sealed memfd探测，所以仅远端推理并把JSON搬回本机，仍不足以通过当前正式校验。迁移还需完整冻结输入、模型/rubric、运行环境、路径绑定、原始响应与校准记录，不能只交一个分数。

普通本机Docker容器共享宿主内核，不能增加缺失的内核能力。若坚持所有步骤都在本机当前内核执行，需要单独设计和审计一个等价的兼容执行后端；直接删flags、退回可写临时脚本或关闭安全校验不构成等价修复。

影响边界：

- 模型解析/生成、检索、原生产物归档、CARLA compile/SV/NE以及可确定的证据提取可以独立推进，仍保留各自的正式准入要求。
- Judge负责确定性提取仍无法判断的语义要求。缺失Judge会使这些要求保持unknown；开发计算中unknown不能当通过或删出分母，不能据此宣称完整语义分数。
- 当前正式score、aggregate、CPD命令和最终结果报告要求可验证Judge运行链；语义符合度及其派生结果、多样性中的要求保留检查和正式发布受阻。
- 把Judge迁到兼容环境也不会自动完成ChatScene自身的正式运行时闭合、平台校准和人工冻结要求。
