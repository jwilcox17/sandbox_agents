import os
import sys
import asyncio
from typing import List, Dict, Any
from contextlib import AsyncExitStack
from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import threading

# Load environment variables
load_dotenv()

# Pydantic models for API
class ConnectRequest(BaseModel):
    server_script_path: str = None

class QueryRequest(BaseModel):
    query: str

class QueryResponse(BaseModel):
    response: str
    status: str = "success"

class ErrorResponse(BaseModel):
    error: str
    status: str = "error"

# Global client instance
global_client = None

# Default server path
DEFAULT_SERVER_PATH = os.path.join(os.path.dirname(__file__), "fantasy_server.py")

# FastAPI app
app = FastAPI(title="Fantasy Baseball MCP Client API", version="1.0.0")

class ToolCallsClient:
    """A streamlined MCP Client that focuses on displaying tool calls clearly"""
    def __init__(self):
        self.session = None
        self.exit_stack = AsyncExitStack()

        # Initialize from environment variables
        self._initialize_from_env()

        self.available_tools = []
        self.message_history = []

        # Add default system prompt
        self.system_prompt = """
You are an advanced AI assistant specialized in fantasy baseball analysis and advice. Your purpose is to provide users with data-driven insights, recommendations, and analysis to help them make optimal decisions for their fantasy baseball teams.

Core Workflow

1. Request Understanding
- Analyze the user's request to identify player names, teams, statistical categories, and analysis type needed.
- Clarify any spelling errors or ambiguities in player names before proceeding.

2. Data Collection (Execute in Order)

For Fantasy League Questions:
- Use read_league_config to get league settings and team names
- Use get_team_roster for mentioned teams to get player names
- Convert all player names to numerical IDs using player search

For Team-Based Questions:
- Search for team ID by team name
- Use get_team_roster to get player names from that team
- Convert all player names to numerical IDs using player search

For Player-Based Questions:
- Search for player IDs directly by player name
- If multiple players share similar names, ask for clarification
- If misspelled, suggest correct name and confirm

3. Statistical Analysis
- ONLY AFTER obtaining all player IDs, query player statistics using these numerical IDs
- Retrieve comprehensive statistical data for all relevant players
- Consider appropriate time periods: recent performance, season-to-date, career, splits

4. Response Formatting (CRITICAL)
- Format all responses using Markdown for readability
- NEVER provide analysis without including actual NUMERICAL statistical data
- ALWAYS display the most relevant NUMERICAL statistics, even if basic
- Present statistical evidence using tables for comparative data and bullet points for highlights
- Use headers to organize different statistical categories
- Include both traditional and advanced metrics when relevant

Response Must Include:
- Player/Team Identification: Confirmation of analyzed subjects (include player ID numbers)
- Statistical Evidence (REQUIRED): Comprehensive statistical data in organized format
- Analysis: Insights based on the statistical evidence
- Recommendation: Clear actionable advice based on analysis

Statistical Categories to Consider

Batting Statistics (ALWAYS SHOW RELEVANT METRICS)
- Traditional: AVG, HR, RBI, R, SB
- Advanced: OBP, SLG, OPS, wOBA, wRC+, BABIP, BB%, K%
- Statcast: Exit Velocity, Launch Angle, Barrel%, Hard Hit%

Pitching Statistics (ALWAYS SHOW RELEVANT METRICS)
- Traditional: W, L, ERA, WHIP, K, SV, HLD
- Advanced: FIP, xFIP, K/9, BB/9, HR/9, SIERA, LOB%
- Statcast: Velocity, Spin Rate, Movement, Whiff%

Additional Guidelines
- Execute tools automatically without asking permission
- Consider league settings, scoring systems, and team needs
- Provide forward-looking insights when appropriate
- Consider factors beyond statistics: lineup position, ballpark factors, injuries, team context
- Briefly explain advanced metrics when central to analysis
- NEVER withhold statistical data - responses must be data-rich and evidence-based

## Example Statistical Display Format

Player Analysis - Recent Performance

Season Statistics
| Statistic | Value | Notes |
|-----------|-------|-------|
| AVG       | .291  | Up from .267 last month |
| OBP       | .397  | Elite plate discipline |
| SLG       | .598  | Power surge in May |
| HR        | 18    | 6 HRs in last 15 games |
| wRC+      | 167   | 67% better than league average |

Recent Performance (Last 15 Games)
- Slash Line: .342/.421/.687
- 6 Home Runs, 15 RBIs, 12 Runs
- 42.3% Hard Hit Rate (up from 38.7% season average)
"""

        # Add initial system message to history
        self.message_history.append({"role": "system", "content": self.system_prompt})

    def _initialize_from_env(self):
        """Initialize client settings from environment variables"""
        # Get user name
        self.user_name = os.getenv("USER_NAME", "")

        # Setup Anthropic client with API key validation
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print("\nERROR: ANTHROPIC_API_KEY not found in environment")
            raise ValueError("ANTHROPIC_API_KEY required")
        self.anthropic = Anthropic(api_key=api_key)

        # Get model
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        print(f"Using model: {self.model}")

        # Get max tokens
        max_tokens_str = os.getenv("MAX_TOKENS", "8000")
        try:
            self.max_tokens = int(max_tokens_str)
        except ValueError:
            print(f"\nERROR: Invalid MAX_TOKENS value: {max_tokens_str}, using default 8000")
            self.max_tokens = 8000

    async def connect_to_server(self, server_script_path: str):
        """Establish connection to MCP server"""
        if not os.path.exists(server_script_path):
            print(f"\nERROR: Server script not found: {server_script_path}")
            raise FileNotFoundError(f"Server script not found: {server_script_path}")

        # Determine if it's a Python or JS server
        is_python = server_script_path.endswith(".py")
        is_js = server_script_path.endswith(".js")
        if not (is_python or is_js):
            print("\nERROR: Invalid server file format. Must be .py or .js")
            raise ValueError("Server file needs to be a .py or .js file")

        command = "python" if is_python else "node"
        server_params = StdioServerParameters(
            command=command, args=[server_script_path], env=None
        )

        # Connect to server
        print("Connecting to server...")
        try:
            stdio_transport = await self.exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            stdio, write = stdio_transport
            self.session = await self.exit_stack.enter_async_context(
                ClientSession(stdio, write)
            )

            # Initialize session and get available tools
            await self.session.initialize()
            response = await self.session.list_tools()
            self.available_tools = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.inputSchema,
                }
                for tool in response.tools
            ]

            print("Connected successfully")
            print(f"Available tools: {[tool['name'] for tool in self.available_tools]}")
        except Exception as e:
            print(f"\nERROR: Connection error: {e}")
            raise ConnectionError(f"Failed to connect to server: {e}")

    async def process_query(self, query: str) -> str:
        """Process user query and generate AI response with focus on tool calls"""
        # Add user message to history
        user_message = {"role": "user", "content": [{"type": "text", "text": query}]}
        self.message_history.append(user_message)

        # Trim message history if it gets too long
        if len(self.message_history) > 50:
            # Keep system messages if present at the beginning
            system_messages = [msg for msg in self.message_history[:2]
                              if msg.get("role") == "system"]
            # Keep most recent messages
            self.message_history = system_messages + self.message_history[-48:]

        final_text = []
        messages = [msg for msg in self.message_history if msg.get("role") != "system"]

        print("Processing...")
        while True:
            try:
                response = self.anthropic.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=messages,
                    tools=self.available_tools,
                    system=self.system_prompt  # Use the system prompt here
                )
            except Exception as e:
                print(f"\nERROR: Error generating response: {e}")
                return f"Error processing your request: {e}"

            # Create a properly structured assistant message
            assistant_message = {"role": "assistant", "content": response.content}
            messages.append(assistant_message)
            self.message_history.append(assistant_message)

            tool_uses = []
            for content in response.content:
                if content.type == "text":
                    final_text.append(content.text)
                elif content.type == "tool_use":
                    # Validate tool exists before adding to tool_uses
                    tool_exists = any(tool["name"] == content.name for tool in self.available_tools)
                    if tool_exists:
                        tool_uses.append(content)
                        # Only print brief information about the tool call (not in output file)
                        print(f"Using tool: {content.name}")
                    else:
                        error_msg = f"Tool '{content.name}' not found in available tools"
                        print(f"\nERROR: {error_msg}")
                        final_text.append(f"\n[ERROR: {error_msg}]\n")

            if not tool_uses:
                break  # No tools used, response is complete

            # Execute tools
            tool_results = []
            try:
                for tool_use in tool_uses:
                    result = await self.session.call_tool(tool_use.name, tool_use.input)

                    # Print minimal tool result information (not in output file)
                    print(f"{tool_use.name} executed successfully")

                    # Add to tool results list
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result.content
                    })

                # Add tool results to messages
                if tool_results:
                    tool_result_message = {"role": "user", "content": tool_results}
                    messages.append(tool_result_message)
                    self.message_history.append(tool_result_message)

            except Exception as e:
                error_msg = f"Tool execution error: {e}"
                print(f"\nERROR: {error_msg}")

                # Create error message for the AI
                error_feedback = {
                    "role": "user",
                    "content": [{"type": "text", "text": f"[ERROR: Tool execution failed: {e}. Please continue without this tool.]"}]
                }
                messages.append(error_feedback)
                self.message_history.append(error_feedback)
                final_text.append(f"\n[ERROR: Tool execution failed: {e}]\n")

                # Don't raise the error, let the conversation continue
                continue

        return "\n".join(final_text)

    async def chat_loop(self):
        """Main interactive chat loop"""
        print("\nAI Assistant ready. Type 'exit' to end.")

        output_file = "client_output.md"
        output_content = []

        while True:
            print(f"\n{self.user_name}>", end="")
            try:
                query = input().strip()
            except EOFError:
                print("\nReceived end of input. Exiting...")
                break

            if query.lower() == "exit":
                break
            elif query.lower() == "/system":
                # Display current system prompt
                print("\nCurrent system prompt:")
                print(f'"{self.system_prompt}"')
                continue
            elif query.lower().startswith("/system:"):
                # Update system prompt
                new_prompt = query[8:].strip()
                if not new_prompt:
                    print("\nPlease enter a new system prompt:")
                    try:
                        new_prompt = input().strip()
                    except EOFError:
                        print("\nInput interrupted. System prompt unchanged.")
                        continue

                if new_prompt:
                    self.system_prompt = new_prompt

                    # Update system message in history or add it if not present
                    system_msg_found = False
                    for i, msg in enumerate(self.message_history):
                        if msg.get("role") == "system":
                            self.message_history[i] = {"role": "system", "content": self.system_prompt}
                            system_msg_found = True
                            break

                    if not system_msg_found:
                        # Insert system message at the beginning
                        self.message_history.insert(0, {"role": "system", "content": self.system_prompt})

                    print("\nSystem prompt updated successfully.")
                else:
                    print("\nSystem prompt update cancelled - empty prompt.")
                continue

            try:
                response = await self.process_query(query)

                print("\nAssistant:")
                print(response)

                # Save only the final conversation to our output content (no tool call details)
                output_content.append(f"## {self.user_name}\n\n{query}\n\n## Assistant\n\n{response}\n\n")

                # Write output to file as we go
                try:
                    with open(output_file, "w", encoding="utf-8") as f:
                        f.write("# Conversation\n\n" + "\n".join(output_content))
                except Exception as e:
                    print(f"\nWARNING: Could not write to output file: {e}")

            except Exception as e:
                print(f"\nERROR: Error in chat loop: {str(e)}")
                print("\nI encountered an error processing your request. Please try again.")

    async def close(self):
        """Clean up and close connections"""
        print("\nClosing connection...")
        try:
            await self.exit_stack.aclose()
            print("Session ended")
        except Exception as e:
            print(f"\nERROR: Error closing session: {e}")
            print("Session ended with errors")

# REST API Endpoints
@app.post("/connect")
async def connect_to_server(request: ConnectRequest = None):
    """Connect to MCP server"""
    global global_client
    try:
        global_client = ToolCallsClient()
        
        # Use provided path or default to fantasy_server.py
        server_path = DEFAULT_SERVER_PATH
        if request and request.server_script_path:
            server_path = request.server_script_path
        
        # Ensure the server path exists
        if not os.path.exists(server_path):
            raise FileNotFoundError(f"Server script not found: {server_path}")
        
        # Check if required dependencies exist
        script_dir = os.path.dirname(server_path)
        required_files = ["fantasy_tools.py", "fantasy_config.py"]
        for req_file in required_files:
            req_path = os.path.join(script_dir, req_file)
            if not os.path.exists(req_path):
                raise FileNotFoundError(f"Required dependency not found: {req_path}")
        
        await global_client.connect_to_server(server_path)
        return {
            "status": "success", 
            "message": f"Connected to server successfully: {server_path}",
            "server_path": server_path,
            "dependencies": required_files
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {str(e)}")

@app.post("/query", response_model=QueryResponse)
async def process_query(request: QueryRequest):
    """Process a query using the connected MCP client"""
    global global_client
    if global_client is None:
        raise HTTPException(status_code=400, detail="No active connection. Connect to server first.")
    
    try:
        response = await global_client.process_query(request.query)
        return QueryResponse(response=response)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query processing failed: {str(e)}")

@app.get("/status")
async def get_status():
    """Get connection status"""
    global global_client
    return {
        "connected": global_client is not None,
        "tools": [tool['name'] for tool in global_client.available_tools] if global_client else []
    }

@app.post("/disconnect")
async def disconnect():
    """Disconnect from server"""
    global global_client
    if global_client:
        await global_client.close()
        global_client = None
        return {"status": "success", "message": "Disconnected from server"}
    else:
        return {"status": "info", "message": "No active connection"}

@app.post("/quick-query", response_model=QueryResponse)
async def quick_query(request: QueryRequest):
    """Connect to default server and process query in one step"""
    global global_client
    
    # Auto-connect if not already connected
    if global_client is None:
        try:
            await connect_to_server()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Auto-connection failed: {str(e)}")
    
    # Process the query
    try:
        response = await global_client.process_query(request.query)
        return QueryResponse(response=response)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query processing failed: {str(e)}")

@app.get("/tools")
async def list_available_tools():
    """Get list of available tools from the connected server"""
    global global_client
    if global_client is None:
        raise HTTPException(status_code=400, detail="No active connection. Connect to server first.")
    
    return {
        "tools": [
            {
                "name": tool['name'],
                "description": tool['description'],
                "input_schema": tool['input_schema']
            }
            for tool in global_client.available_tools
        ]
    }

async def main():
    """Main entry point for the MCP Client application"""
    if len(sys.argv) < 2:
        print("Usage: python fantasy_test_client.py <server_script_path> [--api]")
        print("  --api: Run as REST API server instead of CLI")
        sys.exit(1)

    # Check if API mode is requested
    if "--api" in sys.argv:
        print("Starting Fantasy Baseball MCP Client REST API...")
        uvicorn.run(app, host="0.0.0.0", port=8000)
        return

    server_script_path = sys.argv[1]
    client = ToolCallsClient()

    try:
        await client.connect_to_server(server_script_path)
        await client.chat_loop()
    except KeyboardInterrupt:
        print("\nReceived keyboard interrupt. Shutting down...")
    except Exception as e:
        print(f"Error: {str(e)}")
    finally:
        await client.close()

def run_api():
    """Run the API server"""
    print("Starting Fantasy Baseball MCP Client REST API...")
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    if "--api" in sys.argv:
        run_api()
    else:
        asyncio.run(main())
