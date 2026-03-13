ALTER TABLE approval_requests ADD COLUMN arguments TEXT;
ALTER TABLE approval_requests ADD COLUMN tier INTEGER NOT NULL DEFAULT 2;
