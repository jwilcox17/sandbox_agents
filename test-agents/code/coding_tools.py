import re
import json
import urllib.request
import urllib.parse
import urllib.error
import html


class WebResearchToolkit:
    """A toolkit for researching coding tools, APIs, and ideas from the web."""
    
    def __init__(self):
        self.user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    
    def _make_request(self, url):
        """Make an HTTP request with appropriate headers."""
        headers = {'User-Agent': self.user_agent}
        req = urllib.request.Request(url, headers=headers)
        
        try:
            with urllib.request.urlopen(req) as response:
                return response.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            print(f"HTTP Error: {e.code} - {e.reason}")
        except urllib.error.URLError as e:
            print(f"URL Error: {e.reason}")
        except Exception as e:
            print(f"Error: {e}")
        
        return None

    def search_stackoverflow(self, query):
        """Search Stack Overflow for information about a coding topic."""
        search_query = urllib.parse.quote(f"{query} code examples")
        url = f"https://api.stackexchange.com/2.3/search?order=desc&sort=votes&intitle={search_query}&site=stackoverflow"
        
        try:
            with urllib.request.urlopen(url) as response:
                data = json.loads(response.read().decode('utf-8'))
                
                results = []
                for item in data.get('items', [])[:5]:  # Get top 5 results
                    results.append({
                        'title': html.unescape(item['title']),
                        'link': item['link'],
                        'score': item['score'],
                        'answer_count': item['answer_count'],
                        'tags': item['tags']
                    })
                
                return results
        except Exception as e:
            print(f"Error searching Stack Overflow: {e}")
            return []

    def get_github_readme(self, repo_query):
        """
        Search for a GitHub repository and get its README content.
        repo_query can be in format 'username/repo' or just a library name
        """
        # If the query is not in the format 'username/repo', 
        # we'll try to search for it
        if '/' not in repo_query:
            search_url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(repo_query)}"
            response = self._make_request(search_url)
            
            if response:
                try:
                    data = json.loads(response)
                    if data.get('items') and len(data['items']) > 0:
                        repo_full_name = data['items'][0]['full_name']
                    else:
                        return None
                except json.JSONDecodeError:
                    return None
            else:
                return None
        else:
            repo_full_name = repo_query
            
        # Now try to get the README
        readme_url = f"https://raw.githubusercontent.com/{repo_full_name}/master/README.md"
        readme_content = self._make_request(readme_url)
        
        # If the README.md doesn't exist at master, try main branch
        if not readme_content:
            readme_url = f"https://raw.githubusercontent.com/{repo_full_name}/main/README.md"
            readme_content = self._make_request(readme_url)
            
        # Also check for .rst format
        if not readme_content:
            readme_url = f"https://raw.githubusercontent.com/{repo_full_name}/master/README.rst"
            readme_content = self._make_request(readme_url)
        
        if readme_content:
            return {
                'repo': repo_full_name,
                'readme_url': readme_url,
                'content': readme_content[:2000] + "..." if len(readme_content) > 2000 else readme_content
            }
        
        return None

    def search_pypi(self, package_name):
        """Get information about a Python package from PyPI."""
        url = f"https://pypi.org/pypi/{urllib.parse.quote(package_name)}/json"
        
        try:
            with urllib.request.urlopen(url) as response:
                data = json.loads(response.read().decode('utf-8'))
                
                info = data.get('info', {})
                return {
                    'name': info.get('name'),
                    'version': info.get('version'),
                    'summary': info.get('summary'),
                    'author': info.get('author'),
                    'author_email': info.get('author_email'),
                    'license': info.get('license'),
                    'project_url': info.get('project_url'),
                    'requires_python': info.get('requires_python'),
                    'description': info.get('description')[:500] + "..." if info.get('description') and len(info.get('description')) > 500 else info.get('description')
                }
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"Package '{package_name}' not found on PyPI")
            else:
                print(f"HTTP Error: {e.code} - {e.reason}")
        except Exception as e:
            print(f"Error fetching PyPI data: {e}")
        
        return None

    def extract_code_snippets(self, html_content, language=None):
        """Extract code snippets from HTML content."""
        # Look for code blocks in markdown/HTML
        if language:
            pattern = rf'<pre.*?><code.*?>{language}(.*?)</code></pre>'
            code_blocks = re.findall(pattern, html_content, re.DOTALL | re.IGNORECASE)
            if not code_blocks:
                # Try more generic pattern
                pattern = r'<pre.*?><code.*?>(.*?)</code></pre>'
                code_blocks = re.findall(pattern, html_content, re.DOTALL)
        else:
            pattern = r'<pre.*?><code.*?>(.*?)</code></pre>'
            code_blocks = re.findall(pattern, html_content, re.DOTALL)
            
        # Clean up the code blocks
        cleaned_blocks = []
        for block in code_blocks:
            # Remove HTML entities
            block = html.unescape(block)
            # Remove any remaining HTML tags
            block = re.sub(r'<.*?>', '', block)
            cleaned_blocks.append(block.strip())
            
        return cleaned_blocks

    def search_documentation(self, query, source="python"):
        """
        Search official documentation for information.
        Currently supports Python as a source.
        """
        if source.lower() == "python":
            # We'll use the Python docs search as an example
            search_url = f"https://docs.python.org/3/search.html?q={urllib.parse.quote(query)}&check_keywords=yes&area=default"
            html_content = self._make_request(search_url)
            
            if html_content:
                # Extract search results
                results = []
                
                # Simple pattern to extract search results links and titles
                pattern = r'<a class="reference internal" href="([^"]+)"><span class="std std-ref">([^<]+)</span></a>'
                matches = re.findall(pattern, html_content)
                
                for href, title in matches[:5]:  # Get top 5 results
                    if not href.startswith('http'):
                        href = f"https://docs.python.org/3/{href}"
                    results.append({
                        'title': html.unescape(title),
                        'link': href
                    })
                
                return results
        
        return None

    def find_usage_examples(self, query):
        """Find code examples for a given API or library."""
        # We'll use a simple example of extracting from Stack Overflow
        stackoverflow_results = self.search_stackoverflow(f"{query} example")
        
        examples = []
        for result in stackoverflow_results:
            content = self._make_request(result['link'])
            if content:
                # Try to extract code snippets from the accepted answer
                answer_pattern = r'<div class="answercell[^>]*>.*?<div class="s-prose[^>]*>(.*?)</div>'
                answer_match = re.search(answer_pattern, content, re.DOTALL)
                
                if answer_match:
                    answer_html = answer_match.group(1)
                    code_snippets = self.extract_code_snippets(answer_html)
                    
                    if code_snippets:
                        examples.append({
                            'title': result['title'],
                            'link': result['link'],
                            'code': code_snippets[0]  # Just grab the first snippet
                        })
                        
                        if len(examples) >= 3:  # Limit to 3 examples
                            break
        
        return examples

    def get_comprehensive_info(self, query):
        """
        Get comprehensive information about a coding tool or API.
        This combines multiple data sources.
        """
        results = {
            'query': query,
            'pypi_info': None,
            'github_readme': None,
            'examples': None,
            'documentation': None
        }
        
        # Try PyPI first (if it's a Python package)
        pypi_info = self.search_pypi(query)
        if pypi_info:
            results['pypi_info'] = pypi_info
        
        # Try GitHub
        github_info = self.get_github_readme(query)
        if github_info:
            results['github_readme'] = github_info
        
        # Get usage examples
        examples = self.find_usage_examples(query)
        if examples:
            results['examples'] = examples
        
        # Get documentation links
        docs = self.search_documentation(query)
        if docs:
            results['documentation'] = docs
        
        return results


# Usage examples
if __name__ == "__main__":
    toolkit = WebResearchToolkit()
    
    # Example: Get information about requests library
    info = toolkit.get_comprehensive_info("requests")
    
    # Print PyPI information
    if info['pypi_info']:
        print(f"=== PyPI Information for {info['pypi_info']['name']} ===")
        print(f"Version: {info['pypi_info']['version']}")
        print(f"Summary: {info['pypi_info']['summary']}")
        print()
    
    # Print GitHub README excerpt
    if info['github_readme']:
        print(f"=== GitHub README for {info['github_readme']['repo']} ===")
        print(info['github_readme']['content'][:500] + "..." if len(info['github_readme']['content']) > 500 else info['github_readme']['content'])
        print()
    
    # Print usage examples
    if info['examples']:
        print("=== Usage Examples ===")
        for i, example in enumerate(info['examples'], 1):
            print(f"Example {i} from: {example['title']}")
            print(example['code'])
            print()