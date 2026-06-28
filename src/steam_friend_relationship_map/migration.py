import argparse
import sys
import getpass
from typing import Any

from steam_friend_relationship_map.settings import get_settings
from steam_friend_relationship_map.secrets import SecretStore
from steam_friend_relationship_map.kuzu_repo import KuzuRepositoryImpl
from steam_friend_relationship_map.neo4j_repo import Neo4jRepositoryImpl
from steam_friend_relationship_map.models import ProjectCreate, SteamUserRecord, FriendEdge

def main():
    parser = argparse.ArgumentParser(description="Steam Friend Relationship Map Database Migration Tool")
    parser.add_argument("--from-engine", choices=["kuzu", "neo4j"], required=True, help="Source database engine")
    parser.add_argument("--to-engine", choices=["kuzu", "neo4j"], required=True, help="Target database engine")
    parser.add_argument("--project-id", help="Specify a single project ID to migrate. If omitted, all projects will be migrated.")
    
    # Overrides
    parser.add_argument("--neo4j-uri", help="Override Neo4j connection URI")
    parser.add_argument("--neo4j-user", help="Override Neo4j username")
    parser.add_argument("--neo4j-password", help="Override Neo4j password (will prompt if omitted and needed)")
    parser.add_argument("--kuzu-db-path", help="Override Kuzu database path")
    
    args = parser.parse_args()
    
    if args.from_engine == args.to_engine:
        print("Error: Source and target database engines must be different.")
        sys.exit(1)
        
    settings = get_settings()
    
    # Initialize Source Repository
    print(f"Initializing source engine: {args.from_engine}...")
    if args.from_engine == "kuzu":
        db_path = args.kuzu_db_path or settings.kuzu_db_path
        src_repo = KuzuRepositoryImpl(db_path=db_path, buffer_pool_size_gb=settings.kuzu_buffer_pool_size_gb)
    else:
        uri = args.neo4j_uri or settings.neo4j_uri
        user = args.neo4j_user or settings.neo4j_user
        password = args.neo4j_password
        if not password:
            try:
                password = SecretStore().get("neo4j_password")
            except Exception:
                password = None
        if not password:
            password = getpass.getpass("Enter source Neo4j Password: ")
        src_repo = Neo4jRepositoryImpl(uri=uri, user=user, password=password)
        
    # Initialize Target Repository
    print(f"Initializing target engine: {args.to_engine}...")
    if args.to_engine == "kuzu":
        db_path = args.kuzu_db_path or settings.kuzu_db_path
        dest_repo = KuzuRepositoryImpl(db_path=db_path, buffer_pool_size_gb=settings.kuzu_buffer_pool_size_gb)
    else:
        uri = args.neo4j_uri or settings.neo4j_uri
        user = args.neo4j_user or settings.neo4j_user
        password = args.neo4j_password
        if not password:
            try:
                password = SecretStore().get("neo4j_password")
            except Exception:
                password = None
        if not password:
            password = getpass.getpass("Enter target Neo4j Password: ")
        dest_repo = Neo4jRepositoryImpl(uri=uri, user=user, password=password)
        
    try:
        # Ensure schema in destination
        print("Ensuring target database schema and tables exist...")
        dest_repo.ensure_schema()
        
        # Determine projects to migrate
        projects_to_migrate = []
        if args.project_id:
            projects_to_migrate.append(args.project_id)
        else:
            print("Fetching projects from source...")
            proj_resp = src_repo.list_projects()
            projects_to_migrate = [p.id for p in proj_resp.projects]
            
        if not projects_to_migrate:
            print("No projects found to migrate.")
            return
            
        print(f"Found {len(projects_to_migrate)} projects to migrate: {projects_to_migrate}")
        
        for pid in projects_to_migrate:
            print(f"\n--- Migrating Project: {pid} ---")
            
            # Check if project exists or create it
            if not dest_repo.project_exists(pid):
                print(f"Creating project '{pid}' in target database...")
                p_name = f"Migrated Project {pid}"
                if not args.project_id:
                    for p in proj_resp.projects:
                        if p.id == pid:
                            p_name = p.name
                            break
                dest_repo.create_project(ProjectCreate(name=p_name), project_id=pid)
                
            # Export data from source
            print("Exporting graph data from source...")
            exported = src_repo.export_graph(project_id=pid)
            
            print(f"Exported {len(exported.nodes)} nodes and {len(exported.edges)} relationships.")
            
            # Upsert users in target
            if exported.nodes:
                print("Importing user nodes to target...")
                user_records = []
                for n in exported.nodes:
                    user_records.append(
                        SteamUserRecord(
                            steam_id=n.get("steam_id", ""),
                            persona_name=n.get("persona_name", "Unknown"),
                            profile_url=n.get("profile_url", ""),
                            avatar=n.get("avatar", ""),
                            avatar_medium=n.get("avatar_medium", ""),
                            avatar_full=n.get("avatar_full", ""),
                            visibility_state=n.get("visibility_state"),
                            profile_state=n.get("profile_state"),
                            depth_min=n.get("depth_min", 0),
                            friend_list_status=n.get("friend_list_status", "unknown"),
                            friend_count=n.get("friend_count"),
                            friend_count_status=n.get("friend_count_status", "unknown"),
                            prior_pool_link_count=n.get("prior_pool_link_count", 0),
                            root_closeness_score=n.get("root_closeness_score", 0.0),
                            last_scored_crawl_id=n.get("last_scored_crawl_id", ""),
                        )
                    )
                dest_repo.upsert_users(user_records, project_id=pid)
                
                print("Restoring notes, tags, and categories...")
                for n in exported.nodes:
                    note = n.get("note")
                    tags = n.get("tags")
                    category = n.get("category")
                    if note or tags or category:
                        dest_repo.patch_user(
                            steam_id=n.get("steam_id"),
                            note=note,
                            tags=tags,
                            category=category,
                        )
            
            # Import relationships
            if exported.edges:
                print("Importing relationships to target...")
                edges = []
                for e in exported.edges:
                    edges.append(
                        FriendEdge(
                            from_id=e["source"],
                            to_id=e["target"],
                            crawl_id="migration",
                            source_depth=0,
                        )
                    )
                dest_repo.upsert_relationships(edges, project_id=pid)
                
            print(f"Project '{pid}' migration completed successfully!")
            
        print("\nMigration finished successfully!")
        
    finally:
        src_repo.close()
        dest_repo.close()

if __name__ == "__main__":
    main()
