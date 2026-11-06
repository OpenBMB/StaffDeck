"""Curated OSS starter employees, using the same seed and portable graph contracts."""
import json


def default_staff():
    from app.db.staffdeck_seed import FIXTURE_PATH

    agents = {row['name']: row for row in json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))['agent_profiles']}
    return [{'key': key, 'name': name, 'description': agents[name]['description'],
             'persona_prompt': agents[name].get('persona_prompt') or '',
             'harness_max_actions': 32}
            for key, name in (('it', 'IT'), ('hr', '人事'), ('administration', '行政'),
                              ('finance', '财务'), ('legal', '法务'))]


def default_resource_graph(names):
    """Normalize the shipped curated fixture in isolation, never read a live database.

    Reuse seed schema upgrades and export validation. No users, models, sessions,
    scheduled tasks or credentials enter this graph.
    """
    from sqlmodel import Session, SQLModel, create_engine, select

    from app.db.models import AgentProfile
    from app.db.staffdeck_seed import TENANT_ID, seed_staffdeck_admin_gallery

    from .resources import export_local

    engine = create_engine('sqlite://')
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as db:
            seed_staffdeck_admin_gallery(db, include_expanded=False)
            db.flush()
            ids = [row.id for row in db.exec(select(AgentProfile)).all()
                   if not row.is_overall and row.name in names]
            graph = export_local(db, TENANT_ID, ids)
            graph['records'].pop('ModelConfig', None)
            graph['records'].pop('AgentModelBinding', None)
            return graph
    finally:
        engine.dispose()
