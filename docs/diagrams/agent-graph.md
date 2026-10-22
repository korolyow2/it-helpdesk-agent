# Agent graph

## Request flow

This is the actual compiled outer LangGraph workflow. Its guardrail node
conditionally routes to the existing ReAct graph or a static-response node.
The outer graph has checkpointing disabled; only accepted ReAct turns persist. The classifier has no ticket tools. Its
blocked reply is application-owned. Blocked turns do not change conversation
history or create checkpoints; the intent appears only in diagnostic traces.
A provider refusal can invoke one reserve classifier inside the guardrail node;
exhausted classification failure stops with a technical error. This does not
change the graph topology or grant tools to either classifier.
MCP and the mock independently enforce live user authorization. A repeated
invalid argument or another terminal tool failure ends tool execution; the
model can still explain the failure. One changed-argument correction is allowed.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	guardrail(guardrail)
	agent(agent)
	blocked(blocked)
	__end__([<p>__end__</p>]):::last
	__start__ --> guardrail;
	guardrail -. &nbsp;helpdesk&nbsp; .-> agent;
	guardrail -.-> blocked;
	agent --> __end__;
	blocked --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## Exact compiled graph

The following Mermaid was exported using `build_agent(...).get_graph().draw_mermaid()`
with the same middleware and initial discovery utility as `AgentService`.
An uninvoked local model and in-memory checkpointer were used only to compile
its structure. This export made no provider or MCP requests. The checkpointer
implementation does not change the graph topology; runtime history uses SQLite.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	model(model)
	tools(tools)
	ModelCallLimitMiddleware\2ebefore_model(ModelCallLimitMiddleware.before_model)
	ModelCallLimitMiddleware\2eafter_model(ModelCallLimitMiddleware.after_model)
	ToolCallLimitMiddleware\2eafter_model(ToolCallLimitMiddleware.after_model)
	__end__([<p>__end__</p>]):::last
	ModelCallLimitMiddleware\2eafter_model -.-> ModelCallLimitMiddleware\2ebefore_model;
	ModelCallLimitMiddleware\2eafter_model -.-> __end__;
	ModelCallLimitMiddleware\2eafter_model -.-> tools;
	ModelCallLimitMiddleware\2ebefore_model -.-> __end__;
	ModelCallLimitMiddleware\2ebefore_model -.-> model;
	ToolCallLimitMiddleware\2eafter_model -.-> ModelCallLimitMiddleware\2eafter_model;
	ToolCallLimitMiddleware\2eafter_model -.-> __end__;
	__start__ --> ModelCallLimitMiddleware\2ebefore_model;
	model --> ToolCallLimitMiddleware\2eafter_model;
	tools -.-> ModelCallLimitMiddleware\2ebefore_model;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

Dashed edges are conditional routes; solid edges are unconditional. The call
limit middleware contributes visible nodes. Model/tool wrapper middleware
(context preparation, tracing, dynamic MCP capabilities, and argument validation)
executes around the displayed model/tools and is not a separate graph node.
The input guardrail is a node of the outer workflow shown above.
It is outside the nested compiled ReAct graph shown in this section.
The tool node also handles create/comment proposals through `WriteReview`.
It saves an exact backend proposal and calls native `interrupt`, so its checkpoint
stops inside the existing tools node. A later authenticated decision resumes that
same node using `Command(resume=...)`; no hand-assembled ReAct nodes are added.
A durable application binding prevents restaging on replay. The application
injects the approved ID, and the backend commits one mutation with its durable
replay result. Completion/decline/write-error receipts are application-owned.

```mermaid
flowchart LR
    Model[Model-selected write] --> Stage[MCP stages proposal]
    Stage --> Pause[Interrupt in existing tools node]
    Pause --> Review[Later explicit user decision]
    Review -->|decline| Declined[No execution call]
    Review -->|approve| Resume[Resume exact stored call]
    Resume --> Verify[Backend rechecks approval and access]
    Verify --> Commit[Atomic mutation and replayable result]
```

This second diagram describes the review lifecycle inside the compiled tools
node, not additional compiled graph nodes.

Sources: `backend/src/helpdesk/agent/graph.py`,
`backend/src/helpdesk/agent/workflow.py`,
`backend/src/helpdesk/agent/service.py`, and
`backend/src/helpdesk/agent/guardrails/input.py`.

Raw exports: [compiled graph](agent-compiled.mmd) and
[service flow](agent-request-flow.mmd).
