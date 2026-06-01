#!/usr/bin/env python3
"""
Quick test to identify and fix model display issues
"""
import os
import sys

# Add project root to path  
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('FLASK_ENV', 'production')

def quick_fix_test():
    print("🚀 Quick Fix Test for Model Display...")
    
    try:
        from app import create_app
        
        app = create_app()
        
        with app.app_context():
            db = app.config['MONGODB_DB']
            
            print(f"✅ Connected to database: {db.name}")
            
            # Test 1: Check collections exist
            collections = db.list_collection_names()
            print(f"📋 Collections: {collections}")
            
            # Test 2: Check models collection specifically
            models_count = db.models.count_documents({})
            users_count = db.users.count_documents({})
            print(f"📊 Models: {models_count}, Users: {users_count}")
            
            # Test 3: Check one specific model
            if models_count > 0:
                sample_model = db.models.find_one({})
                print(f"📝 Sample model:")
                print(f"   Name: {sample_model.get('name')}")
                print(f"   Public: {sample_model.get('is_public')} (type: {type(sample_model.get('is_public'))})")
                print(f"   User ID: {sample_model.get('user_id')}")
                
                # Test the exact query that should work
                public_count = db.models.count_documents({'is_public': True})
                print(f"📊 Public models (boolean True): {public_count}")
                
                # Test if it's stored as string
                public_string_count = db.models.count_documents({'is_public': 'true'})
                print(f"📊 Public models (string 'true'): {public_string_count}")
                
                # Test different variations
                public_1_count = db.models.count_documents({'is_public': 1})
                print(f"📊 Public models (number 1): {public_1_count}")
                
            # Test 4: Try Model3D import and methods
            from app.models import Model3D
            
            # Test get_stats
            stats = Model3D.get_stats()
            print(f"📊 get_stats() result: {stats}")
            
            # Test get_public_models  
            public_models, total = Model3D.get_public_models(page=1, per_page=5)
            print(f"📊 get_public_models() result: {total} models")
            
            if total > 0:
                print("✅ FOUND THE ISSUE - models exist and queries work!")
                print("🔧 Problem is likely in template rendering or route handling")
            else:
                print("❌ FOUND THE ISSUE - query is not finding public models")
                print("🔧 Need to fix the is_public field query")
                
                # Try to fix it
                if models_count > 0:
                    print("🔧 Attempting to fix is_public field...")
                    result = db.models.update_many(
                        {'is_public': {'$exists': True}},
                        {'$set': {'is_public': True}}
                    )
                    print(f"   Updated {result.modified_count} models")
                    
                    # Test again
                    fixed_stats = Model3D.get_stats()
                    print(f"📊 After fix - get_stats(): {fixed_stats}")
                    
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    quick_fix_test()
