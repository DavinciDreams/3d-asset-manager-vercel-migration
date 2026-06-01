#!/usr/bin/env python3
"""
Test model retrieval to debug dashboard display issues
"""
import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('FLASK_ENV', 'production')

def test_model_retrieval():
    print("🧪 Testing Model Retrieval for Dashboard Display...")
    
    try:
        from app import create_app
        from app.models import Model3D, User
        
        app = create_app()
        
        with app.app_context():
            print("✅ Step 1: Testing database connection...")
            
            # Test stats
            print("✅ Step 2: Testing get_stats()...")
            stats = Model3D.get_stats()
            print(f"   Stats: {stats}")
            
            # Test public models
            print("✅ Step 3: Testing get_public_models()...")
            public_models, public_total = Model3D.get_public_models(page=1, per_page=6)
            print(f"   Found {public_total} public models")
            for i, model in enumerate(public_models[:3]):  # Show first 3
                print(f"   Model {i+1}: {model.name} (ID: {model.id})")
            
            # Test user models for a specific user
            print("✅ Step 4: Testing get_user_models()...")
            
            # Find a user from your database
            db = app.config['MONGODB_DB']
            user_doc = db.users.find_one({})
            if user_doc:
                user_id = str(user_doc['_id'])
                print(f"   Testing with user ID: {user_id}")
                
                user_models, user_total = Model3D.get_user_models(user_id, page=1, per_page=10)
                print(f"   Found {user_total} user models")
                for i, model in enumerate(user_models[:3]):  # Show first 3
                    print(f"   User Model {i+1}: {model.name} (Public: {model.is_public})")
            else:
                print("   No users found in database")
            
            # Test direct MongoDB query
            print("✅ Step 5: Testing direct MongoDB query...")
            all_models = list(db.models.find({}))
            print(f"   Direct query found {len(all_models)} models total")
            
            for i, model_doc in enumerate(all_models[:2]):  # Show first 2
                print(f"   Raw Model {i+1}:")
                print(f"      Name: {model_doc.get('name')}")
                print(f"      Public: {model_doc.get('is_public')}")
                print(f"      User ID: {model_doc.get('user_id')}")
                print(f"      Upload Date: {model_doc.get('upload_date')}")
            
            print("\n🎯 Diagnosis:")
            if public_total > 0:
                print(f"   ✅ {public_total} public models found - Should show on homepage")
            else:
                print(f"   ⚠️  No public models found - Check is_public field")
                
            if user_total > 0:
                print(f"   ✅ {user_total} user models found - Should show on dashboard")
            else:
                print(f"   ⚠️  No user models found - Check user_id matching")
                
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_model_retrieval()
