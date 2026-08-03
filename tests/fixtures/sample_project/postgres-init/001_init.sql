CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255)
);

CREATE VIEW active_users AS SELECT * FROM users;
