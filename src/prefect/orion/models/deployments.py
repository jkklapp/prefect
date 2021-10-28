"""
Functions for interacting with deployment ORM objects.
Intended for internal use by the Orion API.
"""

import datetime
from typing import List
from uuid import UUID, uuid4

import pendulum
import sqlalchemy as sa
from sqlalchemy import delete, select

import prefect
from prefect.orion import schemas
from prefect.orion.database.dependencies import inject_db_config


@inject_db_config
async def create_deployment(
    session: sa.orm.Session,
    deployment: schemas.core.Deployment,
    db_config=None,
):
    """Upserts a deployment.

    Args:
        session: a database session
        deployment: a deployment model

    Returns:
        db_config.Deployment: the newly-created or updated deployment

    """

    # set `updated` manually
    # known limitation of `on_conflict_do_update`, will not use `Column.onupdate`
    # https://docs.sqlalchemy.org/en/14/dialects/sqlite.html#the-set-clause
    deployment.updated = pendulum.now("UTC")

    insert_stmt = (
        (await db_config.dialect_specific_insert(db_config.Deployment))
        .values(**deployment.dict(shallow=True, exclude_unset=True))
        .on_conflict_do_update(
            index_elements=db_config.deployment_unique_upsert_columns,
            set_=deployment.dict(
                shallow=True,
                include={
                    "schedule",
                    "is_schedule_active",
                    "tags",
                    "parameters",
                    "flow_data",
                    "updated",
                },
            ),
        )
    )

    await session.execute(insert_stmt)

    query = (
        sa.select(db_config.Deployment)
        .where(
            sa.and_(
                db_config.Deployment.flow_id == deployment.flow_id,
                db_config.Deployment.name == deployment.name,
            )
        )
        .execution_options(populate_existing=True)
    )
    result = await session.execute(query)
    model = result.scalar()

    return model


@inject_db_config
async def read_deployment(
    session: sa.orm.Session,
    deployment_id: UUID,
    db_config=None,
):
    """Reads a deployment by id.

    Args:
        session: A database session
        deployment_id: a deployment id

    Returns:
        db_config.Deployment: the deployment
    """

    return await session.get(db_config.Deployment, deployment_id)


@inject_db_config
async def read_deployment_by_name(
    session: sa.orm.Session,
    name: str,
    flow_name: str,
    db_config=None,
):
    """Reads a deployment by name.

    Args:
        session: A database session
        name: a deployment name
        flow_name: the name of the flow the deployment belongs to

    Returns:
        db_config.Deployment: the deployment
    """

    result = await session.execute(
        select(db_config.Deployment)
        .join(db_config.Flow, db_config.Deployment.flow_id == db_config.Flow.id)
        .where(
            sa.and_(db_config.Flow.name == flow_name, db_config.Deployment.name == name)
        )
        .limit(1)
    )
    return result.scalar()


@inject_db_config
async def _apply_deployment_filters(
    query,
    flow_filter: schemas.filters.FlowFilter = None,
    flow_run_filter: schemas.filters.FlowRunFilter = None,
    task_run_filter: schemas.filters.TaskRunFilter = None,
    deployment_filter: schemas.filters.DeploymentFilter = None,
    db_config=None,
):
    """
    Applies filters to a deployment query as a combination of EXISTS subqueries.
    """

    if deployment_filter:
        query = query.where((await deployment_filter.as_sql_filter()))

    if flow_filter:
        exists_clause = select(db_config.Deployment.id).where(
            db_config.Deployment.flow_id == db_config.Flow.id,
            (await flow_filter.as_sql_filter()),
        )

        query = query.where(exists_clause.exists())

    if flow_run_filter or task_run_filter:
        exists_clause = select(db_config.FlowRun).where(
            db_config.Deployment.id == db_config.FlowRun.deployment_id
        )

        if flow_run_filter:
            exists_clause = exists_clause.where((await flow_run_filter.as_sql_filter()))
        if task_run_filter:
            exists_clause = exists_clause.join(
                db_config.TaskRun,
                db_config.TaskRun.flow_run_id == db_config.FlowRun.id,
            ).where((await task_run_filter.as_sql_filter()))

        query = query.where(exists_clause.exists())

    return query


@inject_db_config
async def read_deployments(
    session: sa.orm.Session,
    offset: int = None,
    limit: int = None,
    flow_filter: schemas.filters.FlowFilter = None,
    flow_run_filter: schemas.filters.FlowRunFilter = None,
    task_run_filter: schemas.filters.TaskRunFilter = None,
    deployment_filter: schemas.filters.DeploymentFilter = None,
    db_config=None,
):
    """
    Read deployments.

    Args:
        session: A database session
        offset: Query offset
        limit: Query limit
        flow_filter: only select deployments whose flows match these criteria
        flow_run_filter: only select deployments whose flow runs match these criteria
        task_run_filter: only select deployments whose task runs match these criteria
        deployment_filter: only select deployment that match these filters


    Returns:
        List[db_config.Deployment]: deployments
    """

    query = select(db_config.Deployment).order_by(db_config.Deployment.name)

    query = await _apply_deployment_filters(
        query=query,
        flow_filter=flow_filter,
        flow_run_filter=flow_run_filter,
        task_run_filter=task_run_filter,
        deployment_filter=deployment_filter,
        db_config=db_config,
    )

    if offset is not None:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)

    result = await session.execute(query)
    return result.scalars().unique().all()


@inject_db_config
async def count_deployments(
    session: sa.orm.Session,
    flow_filter: schemas.filters.FlowFilter = None,
    flow_run_filter: schemas.filters.FlowRunFilter = None,
    task_run_filter: schemas.filters.TaskRunFilter = None,
    deployment_filter: schemas.filters.DeploymentFilter = None,
    db_config=None,
) -> int:
    """
    Count deployments.

    Args:
        session: A database session
        flow_filter: only count deployments whose flows match these criteria
        flow_run_filter: only count deployments whose flow runs match these criteria
        task_run_filter: only count deployments whose task runs match these criteria
        deployment_filter: only count deployment that match these filters

    Returns:
        int: the number of deployments matching filters
    """

    query = select(sa.func.count(sa.text("*"))).select_from(db_config.Deployment)

    query = await _apply_deployment_filters(
        query=query,
        flow_filter=flow_filter,
        flow_run_filter=flow_run_filter,
        task_run_filter=task_run_filter,
        deployment_filter=deployment_filter,
        db_config=db_config,
    )

    result = await session.execute(query)
    return result.scalar()


@inject_db_config
async def delete_deployment(
    session: sa.orm.Session, deployment_id: UUID, db_config=None
) -> bool:
    """
    Delete a deployment by id.

    Args:
        session: A database session
        deployment_id: a deployment id

    Returns:
        bool: whether or not the deployment was deleted
    """

    result = await session.execute(
        delete(db_config.Deployment).where(db_config.Deployment.id == deployment_id)
    )
    return result.rowcount > 0


@inject_db_config
async def schedule_runs(
    session: sa.orm.Session,
    deployment_id: UUID,
    start_time: datetime.datetime = None,
    end_time: datetime.datetime = None,
    max_runs: int = None,
    db_config=None,
):
    if max_runs is None:
        max_runs = prefect.settings.orion.services.scheduler_max_runs
    if start_time is None:
        start_time = pendulum.now("UTC")
    start_time = pendulum.instance(start_time)
    if end_time is None:
        end_time = start_time + (
            prefect.settings.orion.services.scheduler_max_scheduled_time
        )
    end_time = pendulum.instance(end_time)

    runs = await _generate_scheduled_flow_runs(
        session=session,
        deployment_id=deployment_id,
        start_time=start_time,
        end_time=end_time,
        max_runs=max_runs,
    )
    return await _insert_scheduled_flow_runs(session=session, runs=runs)


@inject_db_config
async def _generate_scheduled_flow_runs(
    session: sa.orm.Session,
    deployment_id: UUID,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    max_runs: int,
    db_config=None,
) -> List[schemas.core.FlowRun]:
    """
    Given a `deployment_id` and schedule, generates a list of flow run objects and
    associated scheduled states that represent scheduled flow runs. This method
    does NOT insert generated runs into the database, in order to facilitate
    batch operations. Call `_insert_scheduled_flow_runs()` to insert these runs.
    """
    runs = []

    # retrieve the deployment
    deployment = await session.get(db_config.Deployment, deployment_id)

    if not deployment or not deployment.schedule or not deployment.is_schedule_active:
        return []

    dates = await deployment.schedule.get_dates(
        n=max_runs, start=start_time, end=end_time
    )

    for date in dates:
        run = schemas.core.FlowRun(
            flow_id=deployment.flow_id,
            deployment_id=deployment_id,
            parameters=deployment.parameters,
            idempotency_key=f"scheduled {deployment.id} {date}",
            tags=["auto-scheduled"] + deployment.tags,
            auto_scheduled=True,
            state=schemas.states.Scheduled(
                scheduled_time=date,
                message="Flow run scheduled",
            ),
            state_type=schemas.states.StateType.SCHEDULED,
            next_scheduled_start_time=date,
            expected_start_time=date,
        )
        runs.append(run)

    return runs


@inject_db_config
async def _insert_scheduled_flow_runs(
    session: sa.orm.Session,
    runs: List[schemas.core.FlowRun],
    db_config=None,
) -> List[schemas.core.FlowRun]:
    """
    Given a list of flow runs to schedule, as generated by `_generate_scheduled_flow_runs`,
    inserts them into the database. Note this is a separate method to facilitate batch
    operations on many scheduled runs.

    Returns a list of flow runs that were created
    """

    if not runs:
        return []

    # gracefully insert the flow runs against the idempotency key
    # this syntax (insert statement, values to insert) is most efficient
    # because it uses a single bind parameter
    insert = await db_config.dialect_specific_insert(db_config.FlowRun)
    await session.execute(
        insert.on_conflict_do_nothing(
            index_elements=db_config.flow_run_unique_upsert_columns
        ),
        [r.dict(exclude={"created", "updated"}) for r in runs],
    )

    # query for the rows that were newly inserted (by checking for any flow runs with
    # no corresponding flow run states)
    inserted_rows = (
        sa.select(db_config.FlowRun.id)
        .join(
            db_config.FlowRunState,
            db_config.FlowRun.id == db_config.FlowRunState.flow_run_id,
            isouter=True,
        )
        .where(
            db_config.FlowRun.id.in_([r.id for r in runs]),
            db_config.FlowRunState.id.is_(None),
        )
    )
    inserted_flow_run_ids = (await session.execute(inserted_rows)).scalars().all()

    # insert flow run states that correspond to the newly-insert rows
    insert_flow_run_states = [
        {"id": uuid4(), "flow_run_id": r.id, **r.state.dict()}
        for r in runs
        if r.id in inserted_flow_run_ids
    ]
    if insert_flow_run_states:
        # this syntax (insert statement, values to insert) is most efficient
        # because it uses a single bind parameter
        await session.execute(
            db_config.FlowRunState.__table__.insert(), insert_flow_run_states
        )

        # set the `state_id` on the newly inserted runs
        stmt = db_config.set_state_id_on_inserted_flow_runs_statement(
            inserted_flow_run_ids=inserted_flow_run_ids,
            insert_flow_run_states=insert_flow_run_states,
        )

        await session.execute(stmt)

    return [r for r in runs if r.id in inserted_flow_run_ids]
