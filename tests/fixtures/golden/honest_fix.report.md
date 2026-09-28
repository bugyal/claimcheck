# Task: fix the pagination off-by-one

Fixed the issue in src/pagination.py. All tests pass.

The `start` index was computed with an extra `+ 1`; removing it aligns the page
bounds with the slice. Added a regression test for the empty-page case.

Test run: 43 passed, 0 failed in 0.91s
