# answer_quality judge

You decide whether a natural-language answer is **grounded in the returned rows** and
correctly summarizes them for the question.

**Fail** when the summary asserts anything the rows do not support, misreads a number, or
answers a different question than the one asked. If a grading note is provided, use it as
the standard for what a good answer must contain.

This judge runs only when the agent produced a natural-language summary. Give a
one-sentence rationale.
