import os
import asyncio
from dotenv import load_dotenv
# Load environment variables (e.g., API keys)
load_dotenv()

from google.adk.agents import Agent,LoopAgent,SequentialAgent
from google.adk.models.google_llm import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types
from google.adk.tools.tool_context import ToolContext

# Configure retry options for API calls to handle rate limits and transient errors
retry_config = types.HttpRetryOptions(
    attempts=5,
    exp_base=7,
    initial_delay=1,
    http_status_codes= [429,500,503,504]
)

# Collect user inputs for the goal planning session
print("Short Term goal Planner")
print("_"*150)
goal = input("Enter the short term goal that you want to pursue: ")
total_duration = input("Enter the total duration of your goal: ")
free_time = input("Enter the free time that you want to utilize for your goal: ")
current_knowledge = input("Enter the current knowledge that you have related to your goal: ")

# Initialize session service to manage state across agents
session_service = InMemorySessionService()
app_name, session_id, user_id = "agents","default","default"

def exit_loop(tool_context: ToolContext):
    """
Call this function ONLY when the critique is 'APPROVED', indicating the roadmap is accurate and no more changes are needed.
    """
    tool_context.actions.escalate = True
    return {}

async def user_feedback():
    """
    Call this function when the User's feedback is required
    """
    session = await session_service.get_session(
        session_id = session_id,
        app_name=app_name,
        user_id=user_id
    )
    filename = "draft_roadmap.md"

    # Save the current draft roadmap to a file for user review 
    # Note: You should visit disk because the IDE may not refresh the files before completing input operation 
    roadmap = session.state.get("draft_roadmap")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(roadmap)

    # Get absolute path for clarity
    file_path = os.path.abspath(filename)

    print(f"\n[System] Draft saved to: {file_path}")
    print("[System] Type 'APPROVE' to finish, or type your feedback to refine it.")

    loop = asyncio.get_running_loop()
    # Use run_in_executor to make the blocking input() call non-blocking for asyncio
    user_response = await loop.run_in_executor(None, input, ">> Enter response: ")

    # 5. Return explicit instruction to the Agent
    clean_response = user_response.strip()

    if clean_response.upper() == "APPROVE":
        return "User approved the roadmap. Task Complete."
    else:
        # We explicitly label this as feedback so the Agent knows to use it
        return f"User Feedback: {clean_response}. Please update the 'draft_roadmap' based on this."

# Agent 1: Initial Planner
# Implementation: Uses a high-capacity model (Gemini 2.5 Pro) to generate the foundational roadmap.
# Design: This agent acts as the "writer" in the writer-critic loop. It takes raw user requirements and structures them into a markdown format.
# Behavior: It is the entry point of the pipeline, ensuring the subsequent agents have a structured document to critique and refine.
initial_planner_agent = Agent(
    name = "initial_planner_agent",
    model = Gemini(
        model= "gemini-2.5-pro",
        retry_options = retry_config
    ),
    instruction=
    """
    Generate a realistic daily roadmap for the given duration to achieve that goal. You have to generate task based roadmap which include topics to learn and tasks to do on daily basis.
    User's Goal: {goal}
    Total Duration: {total_duration}
    Daily Free Time: {free_time}
    You have to adjust the difficulty of daily roadmap and the level of mastery in the total duration based on user's current knowledge.
    Current knowledge: {current_knowledge}
    
    Roadmap must include daily or bi-weekly practice exercises and it must be adjusted in user's daily free time. 
    Roadmap should me in markdown formating as follow:
    #Title which should gives overview of task to be done (Day No)
    -Task
    -Task
    """,
    output_key= "draft_roadmap"
)

# Agent 2: Critique Agent
# Implementation: Uses a faster, cost-effective model (Gemini 2.5 Flash) for rapid evaluation.
# Design: Acts as the "critic" to ensure the roadmap is realistic and adheres to constraints before the user sees it.
# Behavior: It either approves the plan (ending the loop) or provides specific, actionable feedback for the refiner.
critique_agent = Agent(
    name = "critique_agent",
    model = Gemini(
        model= "gemini-2.5-flash",
        retry_options = retry_config
    ),
    instruction=
    """
    You are a constructive roadmap critic. Evaluate the following roadmap is within daily time limit, include practices bi weekly or weekly, suitable level of depth or not and can be complete in the user's given duration.
    Draft Roadmap: {draft_roadmap}
    -If the roadmap is well-planned and complete, then you must response exact phrase: "APPROVED"
    -Otherwise, provide 2-4 actionable suggestions for improvement.
    """,
    output_key= "critique"
)

# Agent 3: Plan Refiner Agent
# Implementation: Uses Gemini 2.5 Flash to iterate quickly on the roadmap based on critique.
# Design: This agent is the "editor" that applies the changes suggested by the critic.
# Behavior: It has access to the `exit_loop` tool, allowing it to break the refinement cycle once the critique is "APPROVED".
plan_refiner_agent = Agent(
    name="plan_refiner_agent",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=retry_config
    ),
    instruction=
    """
    You are a comprehensive roadmap refine. You have roadmap and critique. 
    Draft Roadmap: {draft_roadmap}
    Critique points: {critique}
    -If the critique is "APPROVED", then you must only call the "exit_loop". DO NOT GENERATE ANY RESPONSE.
    -OTHERWISE, rewrite the draft roadmap to fully incorporate the feedback from the critique.
    
    Roadmap should me in markdown formating as follow:
    #Title which should gives overview of task to be done (Day No)
    -Task
    -Task
    """,
    tools = [
        FunctionTool(exit_loop)
    ],
    output_key="draft_roadmap"
)

# Agent 4: Final Approval Agent
# Implementation: Uses a lightweight model (Flash Lite) to handle the simple logic of asking for user feedback.
# Design: Introduces a "human-in-the-loop" step to ensure the final output meets the user's subjective expectations.
# Behavior: It pauses execution to wait for user input via the `user_feedback` tool and decides whether to loop back or finish.
final_approval_agent = Agent(
    name="final_approval_agent",
    model=Gemini(
        model="gemini-2.5-flash-lite",
        retry_options=retry_config
    ),
    instruction=
    """
    First, must Call the tool "user_feedback".
    -If user's response is roadmap approved and task is complete then you must call the tool "exit_loop".
    -If user provide feedback on roadmap then create 3-4 actionable suggestions to refine the roadmap.
    """,
    output_key= "user_critique",
    tools = [
        FunctionTool(user_feedback),
        FunctionTool(exit_loop)
    ]
)

# Agent 5: Final Refiner Agent
# Implementation: Uses the high-capacity Pro model again to ensure the final polish is of high quality.
# Design: Acts as the final "polisher" that incorporates specific user requests into the already-critiqued roadmap.
# Behavior: It takes the user's feedback and rewrites the roadmap, which is then presented again for approval.
final_refiner_agent = Agent(
    name="final_refiner_agent",
    model=Gemini(
        model="gemini-2.5-pro",
        retry_options=retry_config
    ),
    instruction=
    """
    You are a comprehensive roadmap refine. You have roadmap and critique. 
    Draft Roadmap: {draft_roadmap}
    Critique points: {user_critique}
    -Rewrite the draft roadmap to fully incorporate the feedback from the critique.
    
    Roadmap should me in markdown formating as follow:
    #Title which should gives overview of task to be done (Day No)
    -Task
    -Task
    """,
    output_key="draft_roadmap"
)

# Loop 1: Internal Refinement Loop
# Design: Implements a "Critique-Refine" pattern.
# Behavior: This loop runs autonomously to improve the plan's quality before it is ever shown to the user.
refiner_loop_agent = LoopAgent(
    name="RefinerLoop",
    sub_agents=[critique_agent,plan_refiner_agent]
)

# Loop 2: User Feedback Loop
# Design: Implements a "Human-in-the-loop" pattern.
# Behavior: This loop ensures the process doesn't end until the user explicitly types "APPROVE".
final_refiner = LoopAgent(
    name = "FinalRefiner",
    sub_agents=[final_approval_agent,final_refiner_agent]
)

# Main System: Sequential Execution
# Design: Orchestrates the entire pipeline in a linear stage-by-stage fashion.
# Behavior: 1. Draft -> 2. Auto-Refine -> 3. User-Refine.
planner_system = SequentialAgent(
    name="PlannerSystem",
    sub_agents=[initial_planner_agent,refiner_loop_agent,final_refiner]
)

# Runner to execute the agent system
runner = Runner(
    session_service=session_service,
    app_name=app_name,
    agent=planner_system
)


async def main():
    print("\n--- 1. Initializing Session ---")

    # Initialize the session with user inputs
    # This puts the user's input directly into the agent's memory (state)
    await session_service.create_session(
        session_id=session_id,
        app_name=app_name,
        user_id=user_id,
        state={
            "user_goal": goal,
            "total_duration": total_duration,
            "free_time": free_time,
            "current_knowledge": current_knowledge,
            "draft_roadmap": ""
        }
    )
    print("\n--- 🚀 Pipeline Started ---")

    # Run the agent system
    await runner.run_debug("Start Roadmap Generation",session_id=session_id,user_id=user_id)




if __name__ == "__main__":
    asyncio.run(main())

