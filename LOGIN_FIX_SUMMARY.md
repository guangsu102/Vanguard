# Login Fix Summary

## Issue
The login endpoint at `https://api.rensw.xyz/api/auth/login` was returning a 500 Internal Server Error.

## Root Causes Identified

### 1. Database Query Method Mismatch
**Problem**: The auth.py code was using `db.cursor()` which is for raw database connections, but `get_db()` returns a SQLAlchemy `AsyncSession` object.

**Error**: `AttributeError: 'AsyncSession' object has no attribute 'cursor'`

**Fix**: Updated all database queries in auth.py to use SQLAlchemy's `text()` function with parameterized queries.

### 2. Bcrypt Library Compatibility Issue
**Problem**: The `passlib[bcrypt]` dependency was installing an incompatible version of bcrypt that caused errors with Python 3.12.

**Error**: `ValueError: password cannot be longer than 72 bytes`

**Fix**: 
- Updated `requirements.txt` to use explicit bcrypt version: `bcrypt>=4.0.0`
- Replaced passlib's CryptContext with direct bcrypt usage

### 3. Password Hash Incompatibility
**Problem**: The existing password hash in the database was created with passlib and wasn't compatible with the new bcrypt implementation.

**Fix**: Regenerated the admin user password hash using the new bcrypt method and updated it in the database.

### 4. Missing JWT Configuration
**Problem**: The auth.py code referenced `settings.JWT_SECRET` but the config.py only defined `SECRET_KEY`.

**Error**: `AttributeError: 'Settings' object has no attribute 'JWT_SECRET'`

**Fix**: Added JWT-specific configuration fields to config.py.

## Files Modified

1. **backend/app/api/auth.py** - Updated to use SQLAlchemy text queries and bcrypt directly
2. **backend/requirements.txt** - Updated bcrypt dependency
3. **backend/app/core/config.py** - Added JWT configuration fields
4. **Database** - Updated admin user password hash

## Test Result

✅ **Login endpoint now works successfully**

Response:
```json
{
  "code": 0,
  "message": "登录成功",
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "user": {
      "id": 1,
      "username": "admin",
      "role": "admin",
      "email": "admin@vanguard.local",
      "avatar": null
    }
  }
}
```

## Credentials

- **Username**: admin
- **Password**: admin123
- **URL**: https://api.rensw.xyz/api/auth/login

## Date Fixed
2026-05-24
