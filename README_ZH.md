<h1>avatar-runtime</h1>

<p>
一个用于 <strong>LLM 生成执行计划</strong> 的<strong>最小执行运行时（execution runtime）</strong>。
</p>

<p>
本项目专注于 <strong>确定性执行</strong>、<strong>快速失败（fail-fast）校验</strong> 以及
<strong>基于策略的动作约束</strong> ——  
<strong>不</strong>做规划、不做记忆、不做智能。
</p>

<p>
当一个执行计划存在信息缺失、不安全或语义不明确时，  
该运行时<strong>不会猜测、不会自动修复、也不会悄悄重试</strong>。
</p>

<hr/>

<h2>为什么要做这个项目</h2>

<p>
在真实的 Agent 系统中，大多数失败并不是模型能力不足导致的。
</p>

<p>
问题往往出现在<strong>执行层（execution layer）</strong>：
</p>

<ul>
  <li>参数缺失或结构错误</li>
  <li>执行了不安全或越权的动作</li>
  <li>隐式重试掩盖了真实错误</li>
  <li>执行过程不可回放、不可审计</li>
</ul>

<p>
<strong>avatar-runtime</strong> 将这些问题单独拆出来，  
通过<strong>严格的执行语义</strong>，在“计划”与“真实世界”之间建立一道明确的边界。
</p>

<hr/>

<h2>这个项目是什么</h2>

<ul>
  <li>一个确定性的执行运行时</li>
  <li>一个对结构化计划进行 fail-fast 校验的执行器</li>
  <li>一个用于阻断危险操作的策略层</li>
  <li>一个可追踪、可回放的执行循环</li>
</ul>

<h2>这个项目不是什么</h2>

<ul>
  <li>不是 Agent 框架</li>
  <li>不是 Planner 或任务拆解系统</li>
  <li>不是记忆或上下文系统</li>
  <li>不是自动化工具集</li>
  <li>不是 LLM 封装器</li>
</ul>

<p>
本项目<strong>刻意回避“智能”</strong>，只关注一件事：  
<strong>执行是否正确、是否安全、是否可验证</strong>。
</p>

<hr/>

<h2>快速开始</h2>

<p>克隆仓库并运行演示：</p>

<pre><code>python run_demo.py</code></pre>

<p>演示包含三个执行场景：</p>

<ol>
  <li>
    <strong>成功执行</strong><br/>
    一个简单的文件操作流程，完整执行并产生结果。
  </li>
  <li>
    <strong>参数校验失败</strong><br/>
    当执行步骤缺少必需参数时，在真正执行前立即失败。
  </li>
  <li>
    <strong>策略阻断</strong><br/>
    尝试执行危险操作时，被执行策略明确拒绝。
  </li>
</ol>

<p>
每一次运行都会生成<strong>可回放的执行轨迹（trace artifacts）</strong>，位于：
</p>

<pre><code>./workspace/artifacts/</code></pre>

<hr/>

<h2>演示行为说明</h2>

<ul>
  <li>清晰展示每一步的执行顺序</li>
  <li>在产生副作用前完成参数与结构校验</li>
  <li>明确说明策略拒绝的原因</li>
  <li>为每一次执行生成确定性的 trace 文件</li>
</ul>

<p>示例文件：</p>

<pre><code>trace_success.json
trace_missing_param.json
trace_policy_block.json</code></pre>

<p>
这些 trace 并非普通日志，而是用于<strong>检查、回放和分析</strong>的执行证据。
</p>

<hr/>

<h2>设计原则</h2>

<ul>
  <li><strong>快速失败</strong>：输入不合法时立即终止</li>
  <li><strong>拒绝隐式修复</strong>：不猜、不补、不兜底</li>
  <li><strong>执行前策略校验</strong>：危险动作永远不会被执行</li>
  <li><strong>确定性轨迹</strong>：每次执行都可审计、可复现</li>
</ul>

<hr/>

<h2>项目结构</h2>

<pre><code>runtime/        执行引擎、校验、策略、追踪
skills/         最小内置技能（仅文件相关）
examples/       演示计划与运行入口
run_demo.py     顶层演示入口
workspace/      沙箱执行目录（不纳入版本控制）</code></pre>

<p>项目结构刻意保持克制与显式。</p>

<hr/>

<h2>项目状态</h2>

<p>
本仓库是从一个更大型的 Agent 系统中<strong>抽离出的实验性执行内核</strong>。
</p>

<p>
其目的在于<strong>独立展示执行层的行为</strong>，  
不依赖任何规划器、记忆系统或模型实现。
</p>

<p>
API 尚未稳定，功能范围刻意受限。
</p>

<hr/>

<h2>许可证</h2>

<p>MIT</p>
