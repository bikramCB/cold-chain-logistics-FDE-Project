import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from typing import Annotated, TypedDict

# ==========================================
# 1. SETUP & PATH RESOLUTION
# ==========================================
script_dir = Path(__file__).resolve().parent
project_root = script_dir.parents[0]

if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# HF_HOME must be set before agent_tools imports Hugging Face, or the
# embedding weights land in the default cache on C:.
load_dotenv(project_root / ".env")
hf_home = os.getenv("HF_HOME", r"D:\huggingface_cache")
os.environ["HF_HOME"] = hf_home
os.environ["HF_HUB_CACHE"] = str(Path(hf_home) / "hub")
os.environ["HUGGINGFACE_HUB_CACHE"] = os.environ["HF_HUB_CACHE"]
os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(Path(hf_home) / "sentence_transformers")
Path(hf_home).mkdir(parents=True, exist_ok=True)
print(f"Embedding cache: {hf_home}")

from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver

from src.agent_tools import query_telemetry_db, fetch_corridor_conditions, search_compliance_sop

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]

# ==========================================
# 2. FACTORY INITIALIZATION: AGENT REASONER LLM
# ==========================================

AGENT_LLM_SETTING = os.getenv("Agent_llm", "OLLAMA").strip().upper()

if AGENT_LLM_SETTING == "OPENAI":
    print("🤖 Brain Mode: Utilizing Cloud OpenAI Reasoner (gpt-4o)...")
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model="gpt-4o", temperature=0)

elif AGENT_LLM_SETTING == "DEEPSEEK":
    print("🐳 Brain Mode: Utilizing Flagship DeepSeek Cloud Reasoner (deepseek-v4-pro)...")
    from langchain_openai import ChatOpenAI
    
    llm = ChatOpenAI(
        model="deepseek-v4-flash",
        temperature=0,
        openai_api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
        max_tokens=2048,
    )

elif AGENT_LLM_SETTING == "GEMINI":
    from langchain_google_genai import ChatGoogleGenerativeAI

    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION")
    if not project or not location:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION must be set in .env"
        )

    print(f"Brain Mode: Gemini via Vertex AI [{project} / {location}]...")
    llm = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        temperature=1.0,
        vertexai=True,
        project=project,
        location=location,
        max_output_tokens=2048,
    )

else:  # FALLBACK / DEFAULT RUNNER MODE
    print("🤗 Brain Mode: Local Fallback Activated. Binding Local Ollama (qwen2.5:7b)...")
    from langchain_ollama import ChatOllama
    llm = ChatOllama(model="qwen2.5:7b", temperature=0, num_predict=1024)

fde_tools = [query_telemetry_db, fetch_corridor_conditions, search_compliance_sop]
llm_with_tools = llm.bind_tools(fde_tools)

prompt_path = project_root / "src" / "prompts" / "system_prompt.txt"
try:
    system_instructions = prompt_path.read_text(encoding="utf-8")
except FileNotFoundError:
    system_instructions = (
        "You are a cold-chain dispatcher assistant. Answer in short, plain sentences."
    )

# ==========================================
# 3. GRAPH ARCHITECTURE ASSEMBLY
# ==========================================
def reasoning_node(state: AgentState):
    messages = list(state["messages"])
    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [SystemMessage(content=system_instructions), *messages]
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}

print("⚙️ Compiling LangGraph FDE Orchestrator...")
graph_builder = StateGraph(AgentState)
graph_builder.add_node("reasoner", reasoning_node)
graph_builder.add_node("tools", ToolNode(fde_tools))

graph_builder.add_edge(START, "reasoner")
graph_builder.add_conditional_edges("reasoner", tools_condition)
graph_builder.add_edge("tools", "reasoner")

fde_agent = graph_builder.compile(checkpointer=MemorySaver())

# ==========================================
# 4. CHAT LOOP TESTING PANEL
# ==========================================
if __name__ == "__main__":
    print("\n" + "="*55)
    print("🚀 FDE Supply Chain Orchestrator State Machine Online")
    print(f"   Configured Execution: [LLM: {AGENT_LLM_SETTING}] -> [Embeddings: {os.getenv('Embeddings_model', 'LOCAL')}]")
    print("="*55 + "\n")
    
    # Load the business-structured system prompt from the external file
    prompt_path = project_root / "src" / "prompts" / "system_prompt.txt"
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            system_instructions = f.read()
    except FileNotFoundError:
        print(f"Error: Could not find {prompt_path}")
        system_instructions = "You are a helpful AI assistant." # Basic fallback

    system_prompt = SystemMessage(content=system_instructions)
    
    thread_config = {"configurable": {"thread_id": "production_test_1"}}

    initial_messages = [
        system_prompt, 
        ("user", "System online. Acknowledge and report readiness for dispatch operations.")
    ]

    # Capture the result of the initial invocation
    init_response = fde_agent.invoke({"messages": initial_messages}, config=thread_config)
    if init_response and "messages" in init_response:
        startup_msg = init_response["messages"][-1]
        if startup_msg.content:
            print(f"\n🤖 FDE Agent:\n{startup_msg.content}")
            
    fde_agent.invoke({"messages": initial_messages}, config=thread_config)
    
    from src.guardrails import screen_input

    while True:
        user_input = input("\nDispatcher > ")
        if user_input.lower() in ['exit', 'quit']:
            break

        decision = screen_input(user_input, has_history=True)
        if not decision.allowed:
            print(f"\n🤖 FDE Agent:\n{decision.message}")
            continue

        events = fde_agent.stream({"messages": [("user", user_input)]}, config=thread_config, stream_mode="updates")
        for event in events:
            for node_name, node_state in event.items():
                if node_name == "tools":
                    print("   [System] 🔄 Retrieving external data elements via ToolNode...")
                elif node_name == "reasoner":
                    latest_msg = node_state["messages"][-1]
                    if latest_msg.content:
                        print(f"\n🤖 FDE Agent:\n{latest_msg.content}")