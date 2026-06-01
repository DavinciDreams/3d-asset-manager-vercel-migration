#!/usr/bin/env python3
"""
Minimal MongoDB connection test for Vercel deployment debugging
"""
import os
from pymongo import MongoClient

def test_mongodb_connection():
    print("🔌 Testing MongoDB Connection...")

    mongodb_uri = os.environ.get('MONGODB_URI')
    if not mongodb_uri:
        print("❌ MONGODB_URI is not set")
        print("Set MONGODB_URI to your MongoDB Atlas connection string before running this test.")
        return False
    
    # Test the connection
    try:
        print("\n🧪 Testing MongoDB connection...")
        client = MongoClient(mongodb_uri, serverSelectionTimeoutMS=10000)
        
        # Test connection
        client.admin.command('ping')
        print("✅ MongoDB connection successful!")
        
        # Test database access
        db = client['3d_asset_manager']
        print(f"✅ Database '3d_asset_manager' accessible")
        
        # Test collection creation (just check, don't actually create)
        collections = db.list_collection_names()
        print(f"✅ Can list collections: {len(collections)} collections found")
        
        client.close()
        return True
        
    except Exception as e:
        print(f"❌ MongoDB connection failed: {e}")
        return False

if __name__ == "__main__":
    success = test_mongodb_connection()
    
    if success:
        print("\n🎉 SUCCESS: MongoDB connection works!")
        print("📋 Use this same MONGODB_URI value in Vercel environment variables.")
    else:
        print("\n❌ FAILED: Check your MongoDB Atlas configuration")
        print("📋 Troubleshooting steps:")
        print("1. Verify cluster is running")
        print("2. Check username/password")
        print("3. Verify network access (IP whitelist)")
        print("4. Check database user permissions")
