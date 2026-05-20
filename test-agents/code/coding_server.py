"""MCP server implementation for coding tools and file operations."""

import anyio
import sys
import json
import os
import io
from contextlib import redirect_stdout
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from coding_tools import WebResearchToolkit


class FileTools:
    """Tools for file management operations."""
    
    @staticmethod
    def read_file(file_path):
        """Read content from a file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                content = file.read()
            return {
                'success': True,
                'content': content,
                'file_path': file_path
            }
        except FileNotFoundError:
            return {
                'success': False,
                'error': f"File not found: {file_path}"
            }
        except Exception as e:
            return {
                'success': False,
                'error': f"Error reading file: {str(e)}"
            }
    
    @staticmethod
    def write_file(file_path, content, append=False):
        """Write content to a file."""
        try:
            mode = 'a' if append else 'w'
            with open(file_path, mode, encoding='utf-8') as file:
                file.write(content)
            return {
                'success': True,
                'message': f"{'Appended to' if append else 'Wrote'} file: {file_path}",
                'file_path': file_path
            }
        except Exception as e:
            return {
                'success': False,
                'error': f"Error writing to file: {str(e)}"
            }
    
    @staticmethod
    def list_directory(directory_path='.'):
        """List files in a directory."""
        try:
            files = os.listdir(directory_path)
            file_info = []
            
            for file in files:
                full_path = os.path.join(directory_path, file)
                stats = os.stat(full_path)
                file_info.append({
                    'name': file,
                    'path': full_path,
                    'size': stats.st_size,
                    'is_directory': os.path.isdir(full_path),
                    'modified': stats.st_mtime
                })
            
            return {
                'success': True,
                'directory': directory_path,
                'files': file_info
            }
        except Exception as e:
            return {
                'success': False,
                'error': f"Error listing directory: {str(e)}"
            }


def create_server() -> Server:
    """Create and configure the MCP server with all available tools."""
    app = Server("coding-tools-service")
    web_toolkit = WebResearchToolkit()  # Create a single instance of the web toolkit
    file_tools = FileTools()  # Create a single instance of the file tools

    @app.call_tool()
    async def call_tool(
        name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Handle tool calls by routing to appropriate implementation."""
        try:
            result_text = ""
            
            # Web research tools
            if name == "search_stackoverflow":
                query = arguments.get("query")
                results = web_toolkit.search_stackoverflow(query)
                result_text = json.dumps(results, indent=2)
                
            elif name == "get_github_readme":
                repo = arguments.get("repo")
                readme_info = web_toolkit.get_github_readme(repo)
                
                if readme_info:
                    result_text = f"# README for {readme_info['repo']}\n\n"
                    result_text += readme_info['content']
                else:
                    result_text = f"No README found for repository: {repo}"
                
            elif name == "search_pypi":
                package = arguments.get("package")
                pypi_info = web_toolkit.search_pypi(package)
                
                if pypi_info:
                    result_text = f"# {pypi_info['name']} {pypi_info['version']}\n\n"
                    result_text += f"**Author:** {pypi_info['author']}\n"
                    result_text += f"**License:** {pypi_info['license']}\n"
                    result_text += f"**Summary:** {pypi_info['summary']}\n\n"
                    result_text += pypi_info['description'] if pypi_info['description'] else "No description available."
                else:
                    result_text = f"No PyPI information found for package: {package}"
            
            elif name == "find_usage_examples":
                query = arguments.get("query")
                examples = web_toolkit.find_usage_examples(query)
                
                if examples:
                    result_text = f"# Code Examples for: {query}\n\n"
                    for i, example in enumerate(examples, 1):
                        result_text += f"## Example {i}: {example['title']}\n"
                        result_text += f"Source: {example['link']}\n\n"
                        result_text += f"```python\n{example['code']}\n```\n\n"
                else:
                    result_text = f"No examples found for: {query}"
            
            elif name == "get_comprehensive_info":
                query = arguments.get("query")
                info = web_toolkit.get_comprehensive_info(query)
                
                # Convert to formatted text
                result_text = f"# Information for: {query}\n\n"
                
                if info['pypi_info']:
                    result_text += "## PyPI Information\n"
                    result_text += f"**Name:** {info['pypi_info']['name']}\n"
                    result_text += f"**Version:** {info['pypi_info']['version']}\n"
                    result_text += f"**Summary:** {info['pypi_info']['summary']}\n\n"
                
                if info['github_readme']:
                    result_text += "## GitHub README\n"
                    result_text += f"Repository: {info['github_readme']['repo']}\n\n"
                    readme_content = info['github_readme']['content']
                    if len(readme_content) > 1000:
                        result_text += readme_content[:1000] + "...\n\n"
                    else:
                        result_text += readme_content + "\n\n"
                
                if info['examples'] and len(info['examples']) > 0:
                    result_text += "## Usage Examples\n"
                    for i, example in enumerate(info['examples'], 1):
                        result_text += f"### Example {i}\n"
                        result_text += f"```python\n{example['code']}\n```\n\n"
                
                if info['documentation'] and len(info['documentation']) > 0:
                    result_text += "## Documentation Links\n"
                    for doc in info['documentation']:
                        result_text += f"- [{doc['title']}]({doc['link']})\n"
            
            # File tools
            elif name == "read_file":
                file_path = arguments.get("file_path")
                result = file_tools.read_file(file_path)
                
                if result['success']:
                    result_text = f"# Content of {file_path}\n\n"
                    result_text += result['content']
                else:
                    result_text = f"Error: {result['error']}"
            
            elif name == "write_file":
                file_path = arguments.get("file_path")
                content = arguments.get("content")
                append = arguments.get("append", False)
                
                result = file_tools.write_file(file_path, content, append)
                
                if result['success']:
                    result_text = result['message']
                else:
                    result_text = f"Error: {result['error']}"
            
            elif name == "list_directory":
                directory = arguments.get("directory", ".")
                result = file_tools.list_directory(directory)
                
                if result['success']:
                    result_text = f"# Directory listing for: {result['directory']}\n\n"
                    result_text += "| Name | Type | Size | Modified |\n"
                    result_text += "| ---- | ---- | ---- | -------- |\n"
                    
                    for file in result['files']:
                        file_type = "Directory" if file['is_directory'] else "File"
                        size = f"{file['size']} bytes"
                        import datetime
                        modified = datetime.datetime.fromtimestamp(file['modified']).strftime('%Y-%m-%d %H:%M:%S')
                        
                        result_text += f"| {file['name']} | {file_type} | {size} | {modified} |\n"
                else:
                    result_text = f"Error: {result['error']}"
            
            else:
                raise ValueError(f"Unknown tool: {name}")

            return [types.TextContent(result_text)]

        except Exception as e:
            print(f"Error: Tool call failed - {e}", file=sys.stderr)
            return [types.TextContent(f"Error: {str(e)}")]

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        """List all available tools for coding assistance."""
        return [
            # Web research tools
            types.Tool(
                name="search_stackoverflow",
                description="Search Stack Overflow for code-related questions and answers",
                inputSchema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query for Stack Overflow"
                        }
                    }
                }
            ),
            types.Tool(
                name="get_github_readme",
                description="Get the README content from a GitHub repository",
                inputSchema={
                    "type": "object",
                    "required": ["repo"],
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": "GitHub repository name (username/repo) or library name"
                        }
                    }
                }
            ),
            types.Tool(
                name="search_pypi",
                description="Search for Python package information on PyPI",
                inputSchema={
                    "type": "object",
                    "required": ["package"],
                    "properties": {
                        "package": {
                            "type": "string",
                            "description": "Python package name"
                        }
                    }
                }
            ),
            types.Tool(
                name="find_usage_examples",
                description="Find code examples for a specific library, function, or API",
                inputSchema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The library, function, or API to find examples for"
                        }
                    }
                }
            ),
            types.Tool(
                name="get_comprehensive_info",
                description="Get comprehensive information about a coding tool, API, or library",
                inputSchema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The coding tool, API, or library to research"
                        }
                    }
                }
            ),
            
            # File tools
            types.Tool(
                name="read_file",
                description="Read the contents of a local file",
                inputSchema={
                    "type": "object",
                    "required": ["file_path"],
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to the file to read"
                        }
                    }
                }
            ),
            types.Tool(
                name="write_file",
                description="Write content to a local file",
                inputSchema={
                    "type": "object",
                    "required": ["file_path", "content"],
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to the file to write"
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write to the file"
                        },
                        "append": {
                            "type": "boolean",
                            "description": "Whether to append to the file (true) or overwrite it (false)",
                            "default": False
                        }
                    }
                }
            ),
            types.Tool(
                name="list_directory",
                description="List files and directories in a given directory",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Directory path to list (defaults to current directory)",
                            "default": "."
                        }
                    }
                }
            )
        ]

    return app


def main() -> int:
    """Entry point for the MCP server."""
    print("Starting Coding Tools MCP Server...", file=sys.stderr)

    app = create_server()

    async def arun():
        async with stdio_server() as streams:
            await app.run(streams[0], streams[1], app.create_initialization_options())

    anyio.run(arun)
    return 0


if __name__ == "__main__":
    sys.exit(main())