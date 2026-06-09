SELECT settings_json FROM user_settings
WHERE  user_id = (SELECT user_id FROM projects ORDER BY created_at DESC LIMIT 1);