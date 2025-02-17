# Reflextion Agent

# Basic Import
import time
import datetime

# Langchain Import
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.pydantic_v1 import BaseModel, Field, ValidationError
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.output_parsers.openai_tools import PydanticToolsParser

# OpenAI LLM Import
from langchain_openai import ChatOpenAI

# Tool Import
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_community.utilities.tavily_search import TavilySearchAPIWrapper
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolNode

# Langgraph Import
from typing import Literal
from langgraph.graph import END, MessageGraph

class Reflection(BaseModel):
    missing: str = Field(description="Critique of what is missing.")
    superfluous: str = Field(description="Critique of what is superfluous")
    
class AnswerQuestion(BaseModel):
    """Answer the question. Provide an answer, reflection, and then follow up with search queries to improve the answer."""

    answer: str = Field(description="~250 word detailed answer to the question.")
    reflection: Reflection = Field(description="Your reflection on the initial answer.")
    search_queries: list[str] = Field(
        description="1-3 search queries for researching improvements to address the critique of your current answer."
    )
    
    
class ReviseAnswer(AnswerQuestion):
    """Revise your original answer to your question. Provide an answer, reflection,

    cite your reflection with references, and finally
    add search queries to improve the answer."""

    references: list[str] = Field(
        description="Citations motivating your updated answer."
    )
    
class ResponderWithRetries:
    def __init__(self, runnable, validator):
        self.runnable = runnable
        self.validator = validator

    def respond(self, state: list):
        response = []
        for attempt in range(3):
            response = self.runnable.invoke(
                {"messages": state}, {"tags": [f"attempt:{attempt}"]}
            )
            try:
                self.validator.invoke(response)
                return response
            except ValidationError as e:
                state = state + [
                    response,
                    ToolMessage(
                        content=f"{repr(e)}\n\nPay close attention to the function schema.\n\n"
                        + self.validator.schema_json()
                        + " Respond by fixing all validation errors.",
                        tool_call_id=response.tool_calls[0]["id"],
                    ),
                ]
        return response
search = TavilySearchAPIWrapper()
tavily_tool = TavilySearchResults(api_wrapper=search, max_results=5)
    
class ReflexionMultiAgent:
    """Class defining the reflexion agent framework"""
    
    def __init__(self,llm_model, max_iter=5):
        """Initialize the reflexion framework."""
        self.llm_model = llm_model
        self.graph = self.build_graph(max_iterations=max_iter)
    def create_actor_prompt_template(self):
        actor_prompt_template = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """You are expert researcher.
        Current time: {time}

        1. {first_instruction}
        2. Reflect and critique your answer. Be severe to maximize improvement.
        3. Recommend search queries to research information and improve your answer.""",
                ),
                MessagesPlaceholder(variable_name="messages"),
                (
                    "user",
                    "\n\n<system>Reflect on the user's original question and the"
                    " actions taken thus far. Respond using the {function_name} function.</reminder>",
                ),
            ]
        ).partial(
            time=lambda: datetime.datetime.now().isoformat(),
        )
        return actor_prompt_template
    
    def create_first_responder(self):
        actor_prompt_template = self.create_actor_prompt_template()
        initial_answer_chain = actor_prompt_template.partial(
            first_instruction="Provide a detailed ~250 word answer.",
            function_name=AnswerQuestion.__name__,
        ) | self.llm_model.bind_tools(tools=[AnswerQuestion])

        validator = PydanticToolsParser(tools=[AnswerQuestion])

        first_responder = ResponderWithRetries(
            runnable=initial_answer_chain, validator=validator
        )
        return first_responder
    
    def create_revisor(self):
        actor_prompt_template = self.create_actor_prompt_template()
        revise_instructions = """Revise your previous answer using the new information.
            - You should use the previous critique to add important information to your answer.
                - You MUST include numerical citations in your revised answer to ensure it can be verified.
                - Add a "References" section to the bottom of your answer (which does not count towards the word limit). In form of:
                    - [1] https://example.com
                    - [2] https://example.com
            - You should use the previous critique to remove superfluous information from your answer and make SURE it is not more than 250 words.
        """

        revision_chain = actor_prompt_template.partial(
            first_instruction=revise_instructions,
            function_name=ReviseAnswer.__name__,
        ) | self.llm_model.bind_tools(tools=[ReviseAnswer])
        revision_validator = PydanticToolsParser(tools=[ReviseAnswer])

        revisor = ResponderWithRetries(
            runnable=revision_chain, validator=revision_validator
        )
        return revisor

    def create_tool_node(self):
        def run_queries(search_queries: list[str], **kwargs):
            """Run the generated queries."""
            return tavily_tool.batch([{"query": query} for query in search_queries])

        tool_node = ToolNode(
            [
                StructuredTool.from_function(run_queries, name=AnswerQuestion.__name__),
                StructuredTool.from_function(run_queries, name=ReviseAnswer.__name__),
            ]
        )
        return tool_node

    def build_graph(self, max_iterations: int = 5):
        """Build the reflexion graph."""
        first_responder = self.create_first_responder()
        revisor = self.create_revisor()
        tool_node = self.create_tool_node()

        builder = MessageGraph()
        builder.add_node("draft", first_responder.respond)

        builder.add_node("execute_tools", tool_node)
        builder.add_node("revise", revisor.respond)
        # draft -> execute_tools
        builder.add_edge("draft", "execute_tools")
        # execute_tools -> revise
        builder.add_edge("execute_tools", "revise")

        # Define looping logic:
        def _get_num_iterations(state: list):
            i = 0
            for m in state[::-1]:
                if m.type not in {"tool", "ai"}:
                    break
                i += 1
            return i

        def event_loop(state: list) -> Literal["execute_tools", "__end__"]:
            # in our case, we'll just stop after N plans
            num_iterations = _get_num_iterations(state)
            if num_iterations > max_iterations:
                return END
            return "execute_tools"

        # revise -> execute_tools OR end
        builder.add_conditional_edges("revise", event_loop)
        builder.set_entry_point("draft")
        graph = builder.compile()
        graph.get_graph().draw_mermaid_png(output_file_path="graph.png")
        return graph

    def predict(self, query: str):
        """Generate a response to a user query using Reflexion."""
        outputs = []
        for output in self.graph.stream(
            [HumanMessage(content=query)],
            stream_mode="updates",
        ):
            for key, value in output.items():
                outputs.append(value)
                print(f"Output from node '{key}':")
                print("---")
                print(value)
            print("\n---\n")
            # if "__end__" not in s:
            #     print(s)
            #     print("---")
            #     output.append(s)
            # else:
            #     print("End of stream.")
        # output = self.graph.invoke([HumanMessage(content=query)],
        #                            stream_mode="values",
        #                            )
        return outputs  # ["answer"]#[-1].content
   
    
if __name__ == "__main__":
    # Test the Reflexion class
    llm_model = ChatOpenAI(model_name="gpt-4-turbo-2024-04-09")
    reflexion_class = ReflexionMultiAgent(llm_model=llm_model, max_iter=1)

    user_query = """Write about AI-Powered SOC / autonomous SOC problem domain, list startups that do that and raised capital."""
    response = reflexion_class.predict(query=user_query)
    print(response)