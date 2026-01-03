-- Appwrite Bucket Permissions Fix Script
-- 
-- This script fixes permission issues that can occur after Appwrite upgrades
-- or when buckets are created without proper permission initialization.
--
-- Problem: Files can be uploaded to MinIO but not recorded in the database
-- because the bucket collection permissions are missing.
--
-- Usage: Run against the appwrite database
-- docker exec appwrite-mariadb mysql -uuser -p<password> appwrite < fix-bucket-permissions.sql

-- Check current bucket permissions
SELECT '_1_buckets permissions:' AS info;
SELECT _uid, name, _permissions FROM _1_buckets;

SELECT '_1_buckets_perms:' AS info;
SELECT * FROM _1_buckets_perms;

SELECT '_1__metadata bucket_1 permissions:' AS info;
SELECT _uid, name, _permissions, documentSecurity FROM _1__metadata WHERE _uid = 'bucket_1';

-- Fix: Add bucket-level permissions if missing
-- Replace 'YOUR_BUCKET_ID' with your actual bucket ID (e.g., '6958568a00232dbcb909')

-- INSERT INTO _1_buckets_perms (_type, _permission, _document) VALUES 
-- ('create', 'any', 'YOUR_BUCKET_ID'),
-- ('read', 'any', 'YOUR_BUCKET_ID'),
-- ('update', 'any', 'YOUR_BUCKET_ID'),
-- ('delete', 'any', 'YOUR_BUCKET_ID')
-- ON DUPLICATE KEY UPDATE _permission = VALUES(_permission);

-- Fix: Add collection-level permissions for bucket_1 (file storage collection)
-- This is required for file CRUD operations to work

UPDATE _1__metadata 
SET _permissions = '["create(\\"any\\")", "read(\\"any\\")", "update(\\"any\\")", "delete(\\"any\\")"]'
WHERE _uid = 'bucket_1' AND (_permissions IS NULL OR _permissions = '[]');

-- Add to _1__metadata_perms table
INSERT IGNORE INTO _1__metadata_perms (_type, _permission, _document) VALUES 
('create', 'any', 'bucket_1'),
('read', 'any', 'bucket_1'),
('update', 'any', 'bucket_1'),
('delete', 'any', 'bucket_1');

-- After running this script, flush Redis cache:
-- docker exec appwrite-redis redis-cli FLUSHALL

-- Verify fix
SELECT 'After fix - _1__metadata:' AS info;
SELECT _uid, name, _permissions FROM _1__metadata WHERE _uid = 'bucket_1';

SELECT 'After fix - _1__metadata_perms:' AS info;
SELECT * FROM _1__metadata_perms WHERE _document = 'bucket_1';
