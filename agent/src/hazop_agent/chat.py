"""The viewer's chat assistant: a tool-calling agent over one drawing's HAZOP study.

The conversation thread is the drawing's job id, the same thread id the
pipeline runs under, so a drawing's pipeline runs and its chat sit in one
thread (in LangSmith too). The conversation is kept by a SQLite checkpointer
in the jobs directory, so it survives reloads and server restarts.

The assistant reads the current node breakdown (with the reviewer's edits)
and changes it only through the tools here: merge, rename, undo. Edits are
stored by pipeline/edits.py, beside the pipeline's results.
"""
import asyncio
import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .pipeline import edits, jobs
from .pipeline.llm import model_name

# Tools that change the node breakdown: after one runs, the viewer repaints.
EDIT_TOOLS = {'merge_nodes', 'rename_node', 'undo_last_edit'}


def thread_config(job_id):
    """The run config for a drawing's thread, shared by the pipeline and the chat."""
    return {'configurable': {'thread_id': job_id}, 'metadata': {'thread_id': job_id}}


def _job(config: RunnableConfig):
    return config['configurable']['thread_id']


def _has_nodes(job_id):
    return jobs.has_artifact(job_id, 'step3')


def _node_line(n):
    h = n.get('hazop') or {}
    extra = []
    if h.get('typicalOf'):
        extra.append(f"typical of {h.get('typicalOfName') or h['typicalOf']}")
    if h.get('revisedScope'):
        extra.append('new/modified scope (R6)')
    if n.get('mergedFrom'):
        extra.append('merged from ' + ', '.join(m['label'] for m in n['mergedFrom']))
    equipment = ', '.join(h.get('equipmentTags') or []) or '—'
    return (f"- {n['label']}  [id {n['id']}, page {n.get('page', 0) + 1}]" + (f" ({'; '.join(extra)})" if extra else '')
            + f"\n  equipment: {equipment}\n  intent: {h.get('designIntent') or n.get('description', '')}")


def nodes_summary(job_id):
    if not _has_nodes(job_id):
        return 'This drawing has no pipeline results yet: the nodes appear after "Run HAZOP pipeline".'
    document = edits.current_document(job_id)
    lines = [_node_line(n) for p in document['pages'] for n in p['nodes']]
    return f"{len(lines)} nodes ({edits.view_base(job_id)}):\n" + '\n'.join(lines)


@tool
def list_nodes(config: RunnableConfig) -> str:
    """The drawing's current HAZOP nodes, as the viewer shows them (with any merges or renames)."""
    return nodes_summary(_job(config))


@tool
def show_node(node_id: str, config: RunnableConfig) -> str:
    """Every detail of one node: boundaries, equipment, design intent and conditions, control loops,
    interlocks, relief devices, parameters with safeguards, deviations, operating modes, justification."""
    job_id = _job(config)
    if not _has_nodes(job_id):
        return 'No pipeline results yet.'
    for p in edits.current_document(job_id)['pages']:
        for n in p['nodes']:
            if n['id'] == node_id:
                return json.dumps({'id': n['id'], 'label': n['label'], **(n.get('hazop') or {})}, indent=1)[:12000]
    return f'No node {node_id}. Call list_nodes for the ids.'


@tool
def merge_nodes(node_ids: list[str], new_name: str, reason: str, config: RunnableConfig) -> str:
    """Merges two or more nodes into one: their lines, equipment, interlocks, parameters and deviations
    are combined and the drawing is repainted. The first id survives and keeps its HAZOP worksheet; the
    others' worksheet rows move onto it. new_name: the merged node's name ("" to combine the old names).
    reason: why, in the reviewer's words."""
    job_id = _job(config)
    try:
        edit, document = edits.merge_nodes(job_id, node_ids, new_name, reason)
    except (ValueError, FileNotFoundError) as err:
        return f'Not merged: {err}'
    moved = sum(len(v) for v in edit['movedDeviations'].values())
    survivor = next(n for p in document['pages'] for n in p['nodes'] if n['id'] == node_ids[0])
    return (f"Merged {', '.join(edit['labels'])} into {survivor['label']} (id {survivor['id']})."
            + (f' Moved {moved} worksheet deviation(s) onto it.' if moved else '')
            + f" The drawing now has {sum(len(p['nodes']) for p in document['pages'])} nodes.")


@tool
def rename_node(node_id: str, new_name: str, config: RunnableConfig) -> str:
    """Renames a node (its number is kept)."""
    try:
        edit, _ = edits.rename_node(_job(config), node_id, new_name)
    except (ValueError, FileNotFoundError) as err:
        return f'Not renamed: {err}'
    return f"Renamed {edit['labels'][0]} to {new_name}."


@tool
def undo_last_edit(config: RunnableConfig) -> str:
    """Undoes the latest merge or rename (a merge's worksheet rows go back to their nodes)."""
    edit, document = edits.undo_last(_job(config))
    if not edit:
        return 'There are no edits to undo.'
    what = f"merge of {', '.join(edit['labels'])}" if edit['type'] == 'merge' else f"rename of {edit['labels'][0]}"
    return f"Undid the {what}. The drawing has {sum(len(p['nodes']) for p in document['pages'])} nodes."


@tool
def list_edits(config: RunnableConfig) -> str:
    """The reviewer's edits so far on this drawing, oldest first."""
    job_id = _job(config)
    made = edits.load_edits(job_id, edits.view_base(job_id))
    if not made:
        return 'No edits.'
    return '\n'.join(f"{i}. {e['type']} {', '.join(e['labels'])}" + (f" -> {e['name']}" if e.get('name') else '') + f" ({e['at']})"
                     for i, e in enumerate(made, 1))


@tool
def validation_report(config: RunnableConfig) -> str:
    """The node-rules validation (Step 7) of the pipeline's breakdown: errors, warnings, notes."""
    job_id = _job(config)
    if not _has_nodes(job_id):
        return 'No pipeline results yet.'
    lines = []
    for p in jobs.read_artifact(job_id, 'step3')['pages']:
        v = p['validation']
        lines.append(f"Page {p['pageIndex'] + 1}: {v['counts']}")
        lines += [f"- [{f['severity']}] {f['check']}: {f['detail']}" for f in v['findings']]
    return '\n'.join(lines)[:12000]


TOOLS = [list_nodes, show_node, merge_nodes, rename_node, undo_last_edit, list_edits, validation_report]

SYSTEM = """You are the assistant in a P&ID viewer, helping a PSM engineer review the HAZOP
node breakdown of one drawing. A pipeline has divided the drawing into nodes
(each node is one operation: a tank with its inlets, a transfer train, ...);
the engineer reviews them with you.

You can list and inspect nodes, merge nodes, rename a node, undo the last
edit, and show the validation report. Edits are kept with the drawing and
the viewer repaints after each one.

- Refer to nodes by their number and name as the viewer shows them ("N2
  350-TK-02 HSD receipt…"); use the ids only in tool calls.
- When the engineer asks for an edit naming the nodes plainly ("merge node 1
  and node 2"), do it. If it is ambiguous which nodes are meant, ask first.
- Before merging, say briefly if the merge goes against the node rules: new
  or modified (red) scope merged with existing scope (R6), nodes on different
  operations with different design intents, a node merged with one it is
  typical of. Still do it if the engineer confirms; it is their study.
- Node numbers are stable: a merged node keeps the first node's number and
  the other numbers disappear (N1, N2, N4 after merging N2 and N3). The
  engineer's numbers refer to the nodes as they were when they wrote.
- Make exactly the edits asked for, once. Never make a further edit to
  "correct" one you just made; if something looks wrong, say so and ask.
- After an edit, say what changed in one or two sentences, including any
  worksheet rows that moved.
- Answer questions about the nodes from the tools, not from memory; never
  invent tags or values.
- Keep answers short, in plain text: the chat shows no markdown."""


def _prompt(state, config: RunnableConfig):
    job_id = _job(config)
    manifest = jobs.read_manifest(job_id)
    context = f"Drawing: {manifest.get('fileName') or job_id} (thread {job_id}).\n\nCurrent nodes:\n{nodes_summary(job_id)}"
    return [SystemMessage(SYSTEM + '\n\n' + context), *state['messages']]


_agents = {}
_lock = asyncio.Lock()


async def get_agent():
    """One agent per jobs directory, with its SQLite-checkpointed threads."""
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = jobs.jobs_root() / 'chat.sqlite3'
    async with _lock:
        if path not in _agents:
            path.parent.mkdir(parents=True, exist_ok=True)
            saver = AsyncSqliteSaver(await aiosqlite.connect(str(path)))
            await saver.setup()
            model = ChatOpenAI(model=model_name(), use_responses_api=True)
            # a RunnableLambda, so the prompt gets the config (the thread = the drawing)
            _agents[path] = create_react_agent(model, TOOLS, prompt=RunnableLambda(_prompt), checkpointer=saver, name='hazop-chat')
    return _agents[path]


def _text(message):
    content = message.content
    if isinstance(content, str):
        return content
    return ''.join(part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') in ('text', 'output_text'))


async def history(job_id):
    """The thread's conversation as [{role, text}], tool traffic left out."""
    agent = await get_agent()
    state = await agent.aget_state(thread_config(job_id))
    out = []
    for m in (state.values or {}).get('messages', []):
        if isinstance(m, HumanMessage):
            out.append({'role': 'user', 'text': _text(m)})
        elif isinstance(m, AIMessage) and _text(m).strip():
            out.append({'role': 'assistant', 'text': _text(m)})
    return out


async def chat(job_id, message):
    """One turn. Yields events: {type: tool, name, args}, {type: nodes, document}
    after an edit, and {type: message, text} for the reply."""
    agent = await get_agent()
    config = {**thread_config(job_id), 'run_name': 'hazop-chat'}
    async for update in agent.astream({'messages': [HumanMessage(message)]}, config=config, stream_mode='updates'):
        for step, value in update.items():
            for m in (value or {}).get('messages', []):
                if isinstance(m, AIMessage):
                    for call in m.tool_calls:
                        yield {'type': 'tool', 'name': call['name'], 'args': call['args']}
                    if not m.tool_calls and _text(m).strip():
                        yield {'type': 'message', 'text': _text(m)}
                elif step == 'tools' and getattr(m, 'name', None) in EDIT_TOOLS:
                    yield {'type': 'nodes', 'document': edits.current_document(job_id)}
