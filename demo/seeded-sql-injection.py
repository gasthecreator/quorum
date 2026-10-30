def find_user(db, email):
    query = "SELECT id, email FROM users WHERE email = '" + email + "'"
    return db.execute(query).fetchone()
