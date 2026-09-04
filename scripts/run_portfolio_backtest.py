"""Dated portfolio workflow: use --request and --output-directory.

Legacy undated training-return flags are intentionally no longer accepted.
See docs/state-and-portfolio-workflow.md.
"""
from honest_alpha_lab.portfolio_workflow import main

if __name__ == "__main__":
    main()
