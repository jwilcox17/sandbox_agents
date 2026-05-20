import os
import sys
import json
import re
import logging
import asyncio
import argparse
from typing import Dict, List, Optional, Tuple, Union, Any
from contextlib import AsyncExitStack
from datetime import datetime
from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("coding_client")

# Maximum number of messages to keep in history
MAX_HISTORY_LENGTH = 50

class CodingAssistantClient:
    """
    An intelligent coding assistant client that integrates with the MCP coding server
    to provide a comprehensive coding assistance workflow.
    """
    def __init__(self, verbose: bool = False, server_path: Optional[str] = None):
        """
        Initialize the coding assistant client.

        Args:
            verbose: Whether to print verbose logs
            server_path: Path to the server script
        """
        self.server_path = server_path
        self.session = None
        self.exit_stack = AsyncExitStack()
        self.verbose = verbose
        self.available_tools = []
        self.message_history = []
        self.output_dir = os.path.join(os.getcwd(), "generated_code")

        # Create output directory if it doesn't exist
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        # Initialize from environment
        self._initialize_from_env()

        self.log("Coding Assistant initialized.")

    def _initialize_from_env(self):
        """Initialize client settings from environment variables with validation"""
        # Get user name
        self.user_name = os.getenv("USER_NAME", "User")

        # Setup Anthropic client with API key validation
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            self._log_error("ANTHROPIC_API_KEY not found in environment")
            raise ValueError("ANTHROPIC_API_KEY required")
        self.anthropic = Anthropic(api_key=api_key)

        # Get model with validation
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")
        logger.info(f"Using model: {self.model}")

        # Get max tokens with validation
        max_tokens_str = os.getenv("MAX_TOKENS", "4000")
        try:
            self.max_tokens = int(max_tokens_str)
        except ValueError:
            self._log_error(f"Invalid MAX_TOKENS value: {max_tokens_str}, using default 4000")
            self.max_tokens = 4000

    def _log_error(self, message: str):
        """Log error messages"""
        print(f"\nERROR: {message}")
        logger.error(message)

    def log(self, message: str):
        """Print a log message if verbose mode is enabled."""
        if self.verbose:
            print(f"[LOG] {message}")
            logger.info(message)

    def _extract_code_blocks(self, text: str) -> List[str]:
        """Extract code blocks from markdown text"""
        pattern = r'```(?:[\w+-]*)\n([\s\S]*?)\n```'
        return re.findall(pattern, text)

    def _format_text_for_raw_output(self, text: str) -> str:
        """Format text for raw text output"""
        # Replace markdown code blocks with a clearer format
        pattern = r'```(?:[\w+-]*)\n([\s\S]*?)\n```'

        def replace_code_block(match):
            code = match.group(1)
            return f"\n--- CODE BLOCK START ---\n{code}\n--- CODE BLOCK END ---\n"

        processed_text = re.sub(pattern, replace_code_block, text)
        return processed_text

    def _trim_message_history(self):
        """Trim message history to prevent excessive memory usage"""
        if len(self.message_history) > MAX_HISTORY_LENGTH:
            # Keep the first system message if present, then truncate older messages
            system_messages = [msg for msg in self.message_history[:2]
                              if msg.get("role") == "system"]

            # Keep the most recent messages plus any system messages
            self.message_history = (
                system_messages +
                self.message_history[-(MAX_HISTORY_LENGTH-len(system_messages)):]
            )
            logger.info(f"Trimmed message history to {len(self.message_history)} messages")

    async def connect_to_server(self):
        """Establish connection to MCP server"""
        if not self.server_path:
            self._log_error("Server path not provided")
            raise ValueError("Server path required")

        if not os.path.exists(self.server_path):
            self._log_error(f"Server script not found: {self.server_path}")
            raise FileNotFoundError(f"Server script not found: {self.server_path}")

        is_python = self.server_path.endswith(".py")
        is_js = self.server_path.endswith(".js")
        if not (is_python or is_js):
            self._log_error("Invalid server file format. Must be .py or .js")
            raise ValueError("Server file needs to be a .py or .js file")

        command = "python" if is_python else "node"
        server_params = StdioServerParameters(
            command=command, args=[self.server_path], env=None
        )

        # Connect with feedback
        print("Connecting to server...")
        try:
            stdio_transport = await self.exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            stdio, write = stdio_transport
            self.session = await self.exit_stack.enter_async_context(
                ClientSession(stdio, write)
            )

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
            logger.info(f"Found these tools: {[tool['name'] for tool in self.available_tools]}")
        except Exception as e:
            self._log_error(f"Connection error: {e}")
            raise ConnectionError(f"Failed to connect to server: {e}")

    async def understand_request(self, query: str) -> Tuple[str, str]:
        """
        Initial step to understand the user request before processing.
        This enhanced version:
        1. Corrects misspellings and grammar errors
        2. Expands on general ideas
        3. Provides a clear interpretation of the user's intent

        Returns:
            Tuple containing:
            - The interpreted/corrected query (for processing)
            - Analysis text explaining the understanding
        """
        print("\nAnalyzing your request...")

        # Create a robust system message for understanding and correction
        system_content = """
        Your first task is to carefully analyze the user's request before taking any action. This involves:

        1. CORRECTION: Identify and correct any misspellings, grammar errors, or unclear phrasing that might cause misunderstanding.

        2. EXPANSION: If the user provides general or vague ideas, expand on these with relevant details and possibilities.

        3. INTERPRETATION: Provide a clear interpretation of what you believe the user is asking for.

        4. CLARIFICATION NEEDS: If critical information is missing, identify what needs to be clarified.

        Structure your response in these sections:
        - CORRECTED REQUEST: [Provide the corrected version of the query]
        - ANALYSIS: [Your detailed understanding of the request]

        Do not take any actions or make tool calls at this stage - focus only on understanding.
        """

        # Set up messages with the user query only, system message as top-level parameter
        understanding_messages = [{"role": "user", "content": query}]

        try:
            understanding_response = self.anthropic.messages.create(
                model=self.model,
                max_tokens=1000,  # Increased for more detailed understanding
                messages=understanding_messages,
                system=system_content,  # Use system as a top-level parameter
                temperature=0.1,  # Lower temperature for more precise correction
            )

            # Extract understanding text
            understanding_text = ""
            corrected_query = query  # Default to original if parsing fails

            for content in understanding_response.content:
                if content.type == "text":
                    understanding_text += content.text

            # Try to extract the corrected request section
            corrected_match = re.search(r'CORRECTED REQUEST:(.*?)(?:ANALYSIS:|$)',
                                    understanding_text, re.DOTALL)
            if corrected_match:
                corrected_query = corrected_match.group(1).strip()
            else:
                logger.warning("Failed to extract CORRECTED REQUEST section. Using original query.")

            # Log the understanding
            print("\n--- UNDERSTANDING YOUR REQUEST ---")
            print(understanding_text)
            print("--- END OF UNDERSTANDING ---\n")

            # Return both the corrected query and the analysis
            return corrected_query, understanding_text

        except Exception as e:
            self._log_error(f"Error understanding request: {e}")
            return query, f"Failed to analyze request due to error: {e}"

    async def process_query(self, query: str) -> str:
        """Process user query and generate AI response using the available tools"""
        # First step: understand the request, correct spelling/grammar and expand ideas
        corrected_query, understanding = await self.understand_request(query)

        # Add both original and corrected query to context
        context_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": query},
                {"type": "text", "text": f"\n[SYSTEM: The corrected and expanded interpretation of your request is: {corrected_query}]"}        
            ]
        }
        self.message_history.append(context_message)

        # Trim message history to prevent memory issues
        self._trim_message_history()

        logger.info(f"Original query: {query}")
        logger.info(f"Processed query: {corrected_query}")

        final_text = []
        messages = self.message_history.copy()

        print("Working on your request...")
        while True:
            try:
                response = self.anthropic.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=messages,
                    tools=self.available_tools,
                )
            except Exception as e:
                self._log_error(f"Error generating response: {e}")
                return f"I encountered an error while processing your request: {e}"

            assistant_message = {"role": "assistant", "content": response.content}
            messages.append(assistant_message)
            self.message_history.append(assistant_message)
            self._trim_message_history()

            tool_uses = []
            for content in response.content:
                if content.type == "text":
                    final_text.append(content.text)
                elif content.type == "tool_use":
                    # Validate tool exists before adding to tool_uses
                    tool_exists = any(tool["name"] == content.name for tool in self.available_tools)
                    if tool_exists:
                        tool_uses.append(content)
                        # Add tool usage indicator
                        tool_marker = f"\n[USING TOOL: {content.name}]\n"
                        final_text.append(tool_marker)
                        logger.info(f"Running tool: {content.name} with args {content.input}")
                    else:
                        error_msg = f"Tool '{content.name}' not found in available tools"
                        self._log_error(error_msg)
                        final_text.append(f"\n[ERROR: {error_msg}]\n")

            if not tool_uses:
                break  # No tools used, response is complete

            # Execute tools
            try:
                tool_calls = [self.session.call_tool(tool_use.name, tool_use.input) for tool_use in tool_uses]
                results = await asyncio.gather(*tool_calls)

                tool_results = [
                    {"type": "tool_result", "tool_use_id": tool_use.id, "content": result.content}
                    for tool_use, result in zip(tool_uses, results)
                ]
                tool_result_message = {"role": "user", "content": tool_results}
                messages.append(tool_result_message)
                self.message_history.append(tool_result_message)
                self._trim_message_history()

            except Exception as e:
                error_msg = f"Tool execution error: {e}"
                self._log_error(error_msg)

                # Create error message for the AI
                error_feedback = {
                    "role": "user",
                    "content": [{"type": "text", "text": f"[ERROR: Tool execution failed: {e}. Please continue without this tool.]"}]
                }
                messages.append(error_feedback)
                self.message_history.append(error_feedback)
                self._trim_message_history()
                final_text.append(f"\n[ERROR: Tool execution failed: {e}]\n")

                # Don't raise the error, let the conversation continue
                continue

        return "\n".join(final_text)

    async def save_solution(self, solution: str, query: str) -> str:
        """
        Save a code solution to a file using the write_file tool.

        Args:
            solution: The code solution to save
            query: The original query that led to this solution

        Returns:
            The path to the saved file or error message
        """
        self.log("Saving solution...")

        # Extract code blocks from the solution
        code_blocks = self._extract_code_blocks(solution)
        if not code_blocks:
            return "No code blocks found to save"

        # Use the first code block
        code_to_save = code_blocks[0]

        # Create a suitable filename based on the query
        # Extract first few words of the query for the filename
        words = query.split()[:3]
        filename_base = "_".join([w.lower() for w in words if w.isalnum()])

        # Add timestamp to make the filename unique
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Determine the file extension based on the content
        if "import java" in code_to_save or "public class" in code_to_save:
            extension = ".java"
        elif "function" in code_to_save and ("console.log" in code_to_save or "require(" in code_to_save):
            extension = ".js"
        else:
            extension = ".py"  # Default to Python

        filename = f"{filename_base}_{timestamp}{extension}"
        filepath = os.path.join(self.output_dir, filename)

        # Find the appropriate tool
        write_file_tool = next((tool for tool in self.available_tools if tool["name"] == "write_file"), None)
        if not write_file_tool:
            return "The write_file tool is not available on this server"

        # Save the file using the write_file tool through process_query
        save_query = f"Save this code to the file {filepath}:\n```\n{code_to_save}\n```"
        result = await self.process_query(save_query)

        if "successfully" in result.lower():
            print(f"\nSolution saved to: {filepath}")
            return filepath
        else:
            print(f"\nError saving solution: {result}")
            return "Failed to save the solution"

    async def chat_loop(self):
        """Main interactive chat loop for the coding assistant"""
        print("\n===================================")
        print("Welcome to the MCP Coding Assistant!")
        print("===================================\n")
        print("I can help you implement coding solutions, optimize existing code,")
        print("or provide explanations for programming concepts.")
        print("Type 'exit' to end the session or 'save' to save the latest code.\n")

        last_response = ""  # Track the last response to support saving

        while True:
            print(f"\n{self.user_name}: ", end="")
            query = input().strip()

            if query.lower() == "exit":
                break

            if query.lower() == "save" and last_response:
                # Extract and save code blocks from the last response
                print("\nSaving the last code solution...")
                filepath = await self.save_solution(last_response, "last_code_solution")
                print(f"Result: {filepath}")
                continue

            try:
                response = await self.process_query(query)
                last_response = response  # Save the response for potential saving later

                # Format response for raw text output
                formatted_response = self._format_text_for_raw_output(response)

                print("\nAssistant:")
                print(formatted_response)

                # If code blocks are present, offer to show raw code
                code_blocks = self._extract_code_blocks(response)
                if code_blocks:
                    print("\nCode blocks found. Type 'c' to view raw code for copying, 's' to save, or any other key to continue.")
                    key = input().lower()

                    if key == 'c':
                        print("\nRaw code for copying:")
                        for i, code in enumerate(code_blocks):
                            print(f"\nBlock {i+1}:")
                            print(code)  # Print raw code without formatting
                    elif key == 's':
                        print("\nSaving code...")
                        filepath = await self.save_solution(response, query)
                        print(f"Result: {filepath}")

            except Exception as e:
                self._log_error(f"Error in chat loop: {str(e)}")
                print("\nI encountered an error processing your request. Please try again or rephrase your query.")

    async def close(self):
        """Clean up and close connections"""
        print("\nClosing connection...")
        try:
            await self.exit_stack.aclose()
            print("Session ended")
        except Exception as e:
            self._log_error(f"Error closing session: {e}")
            print("Session ended with errors")

async def main_async(args):
    """Async entry point for the MCP Coding Assistant Client."""
    client = CodingAssistantClient(verbose=args.verbose, server_path=args.server)

    try:
        await client.connect_to_server()
        await client.chat_loop()
    except KeyboardInterrupt:
        print("\nReceived keyboard interrupt. Shutting down...")
    except Exception as e:
        print(f"Error: {str(e)}")
    finally:
        await client.close()

def main():
    """Entry point for the MCP Coding Assistant Client."""
    parser = argparse.ArgumentParser(description="MCP Coding Assistant Client")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("server", help="Path to the MCP server script (.py or .js)")
    args = parser.parse_args()

    asyncio.run(main_async(args))

if __name__ == "__main__":
    main()