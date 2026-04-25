import asyncpg
import asyncio

async def check():
    print("Trying PostgreSQL connections...")
    
    # Try with a simpler connection string
    try:
        conn = await asyncpg.connect(
            host='localhost',
            port=5432,
            database='postgres',
            user='postgres'
        )
        print("Connected to postgres database")
        await conn.close()
    except asyncpg.exceptions.InvalidPasswordError:
        print("postgres user needs correct password")
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(check())
