# sqbyl demo — a ~20s NL → SQL → rows clip on the bundled DuckDB dogfood project.
#
# Render it (produces docs/assets/demo.gif):
#   1. Install VHS: https://github.com/charmbracelet/vhs   (`brew install vhs`)
#   2. Install sqbyl with a provider:  pip install 'sqbyl[anthropic]'  (or [openai])
#   3. Export a key (this spends a few cents):
#        export ANTHROPIC_API_KEY=sk-ant-...        # or OPENAI_API_KEY
#   4. From the repo root:  vhs docs/demo.tape
#
# Then commit docs/assets/demo.gif and wire it into the README (see the commented
# <img> slot near the top of README.md).

Output docs/assets/demo.gif
Require sqbyl

Set Shell zsh
Set FontSize 18
Set Width 1200
Set Height 640
Set Padding 28
Set Theme "Catppuccin Mocha"

# Hidden setup: point at the bundled fixture from inside the dogfood project.
Hide
Type "export DATABASE_URL=duckdb:///$(pwd)/fixtures/orders.duckdb" Enter
Type "cd examples/revenue-analytics" Enter
Type "clear" Enter
Show

Sleep 800ms
Type "sqbyl ask 'which 5 customers spent the most in 2024?'" Sleep 600ms Enter
Sleep 8s
