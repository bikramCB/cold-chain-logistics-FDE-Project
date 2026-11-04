import os

from deepeval import assert_test
from deepeval.test_case import LLMTestCase
from deepeval.metrics import ContextualRelevancyMetric, FaithfulnessMetric
from deepeval.models import GeminiModel
from src.agent_tools import search_compliance_sop

def test_sop_rag_pipeline():
    # 1. User question simulating a dispatcher query
    user_query = "What is the hard trigger for Tier 2 Logistics Manager escalation?"
    
    # 2. Retrieve context from Pinecone using your existing tool
    retrieved_context_response = search_compliance_sop.invoke(user_query)
    
    # 3. Simulate the LLM's generated answer based on that context
    actual_output = (
        "The hard trigger for Tier 2 escalation is when a shipment is classified as "
        "'High Risk' combined with a delay probability greater than 0.65."
    )
    
    # 4. Define DeepEval test case
    test_case = LLMTestCase(
        input=user_query,
        actual_output=actual_output,
        retrieval_context=[retrieved_context_response]
    )
    
    # 5. Judge with the same Vertex Gemini model the agent uses
    judge = GeminiModel(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GOOGLE_CLOUD_LOCATION"),
        use_vertexai=True,
    )
    faithfulness_metric = FaithfulnessMetric(threshold=0.7, model=judge)
    context_relevancy_metric = ContextualRelevancyMetric(threshold=0.7, model=judge)
    
    # Execute evaluation assertions
    assert_test(test_case, [faithfulness_metric, context_relevancy_metric])