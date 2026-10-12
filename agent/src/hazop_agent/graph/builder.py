"""The HAZOP node agent graph.

    START -> ingest -> extract -> trace -> classify -> sheet_data -> cluster -> [consolidate] -> END

One node per pipeline step. Every edge after a step checks `failure` first,
so an unusable result (no entities, no processes) stops the run instead of
paying for the steps after it. `consolidate` only runs when `max_nodes` is set.
"""
from langgraph.graph import END, START, StateGraph

# Absolute imports: `langgraph dev` loads this file by path (langgraph.json),
# outside the package, where relative imports fail.
from hazop_agent.graph.nodes import classify, cluster, consolidate_nodes, extract, ingest, sheet_data, trace
from hazop_agent.graph.state import HazopInput, HazopState


def _next_or_end(next_node):
    def route(state: HazopState):
        return END if state.get('failure') else next_node
    return route


def _after_cluster(state: HazopState):
    if state.get('failure') or not state.get('max_nodes'):
        return END
    return 'consolidate'


def build_graph(checkpointer=None):
    graph = StateGraph(HazopState, input_schema=HazopInput)
    graph.add_node('ingest', ingest)
    graph.add_node('extract', extract)
    graph.add_node('trace', trace)
    graph.add_node('classify', classify)
    graph.add_node('sheet_data', sheet_data)
    graph.add_node('cluster', cluster)
    graph.add_node('consolidate', consolidate_nodes)

    graph.add_edge(START, 'ingest')
    graph.add_conditional_edges('ingest', _next_or_end('extract'), ['extract', END])
    graph.add_conditional_edges('extract', _next_or_end('trace'), ['trace', END])
    graph.add_conditional_edges('trace', _next_or_end('classify'), ['classify', END])
    graph.add_conditional_edges('classify', _next_or_end('sheet_data'), ['sheet_data', END])
    graph.add_conditional_edges('sheet_data', _next_or_end('cluster'), ['cluster', END])
    graph.add_conditional_edges('cluster', _after_cluster, ['consolidate', END])
    graph.add_edge('consolidate', END)
    return graph.compile(checkpointer=checkpointer)


graph = build_graph()
