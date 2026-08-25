"""
Guardian Bot Entry Point

Run with: python -m app.modules.guardian
"""

import asyncio


async def main():
    """Main entry point for guardian bot."""
    print("Guardian Bot - Telegram Group Moderation")
    print("=" * 50)
    print("This module provides:")
    print("  - Message moderation (rule engine)")
    print("  - Anti-spam (frequency, repeated content)")
    print("  - Competitor blocking")
    print("  - User punishment management")
    print("  - Group join verification")
    print("  - Broadcasting")
    print("  - Coupon/reward distribution")
    print()
    print("Usage: Import GuardianBot from main.py")
    print()


if __name__ == "__main__":
    asyncio.run(main())
