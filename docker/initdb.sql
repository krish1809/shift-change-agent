-- Separate databases so tests and eval never touch the demo data.
CREATE DATABASE shift_test OWNER shift;
CREATE DATABASE shift_eval OWNER shift;
