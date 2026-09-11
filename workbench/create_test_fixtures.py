"""Create only development fixtures. Must run against the isolated wb-postgres database."""
import json
import os
from pathlib import Path
import secrets

from app import app
from configs import dify_config
from core.db.session_factory import session_factory
from libs.datetime_utils import naive_utc_now
from models import Account, TenantAccountJoin
from models.account import TenantAccountRole
from models.agent import Agent, AgentConfigSnapshot
from models.agent_config_entities import AgentSoulConfig
from services.account_service import AccountService
from services.agent.roster_service import AgentRosterService
from services.agent.composer_service import AgentComposerService
from services.entities.agent_entities import ComposerSavePayload
from sqlalchemy import select

assert dify_config.DB_HOST == 'wb-postgres', 'Development fixtures require the isolated database'
output=Path('/fixtures/accounts.json')
saved=json.loads(output.read_text()) if output.exists() else {'accounts':[]}
with app.app_context(), app.test_request_context():
    with session_factory.create_session() as session:
        owner=session.scalar(select(TenantAccountJoin).where(TenantAccountJoin.role==TenantAccountRole.OWNER))
        tenant_id,owner_id=owner.tenant_id,owner.account_id
        for index in range(1,6):
            email=f'workbench-test-{index}@example.com'
            account=session.scalar(select(Account).where(Account.email==email))
            if account is None:
                password='Wb9!'+secrets.token_urlsafe(18)
                account=AccountService.create_account(email=email,name=f'工作台测试 {index}',interface_language='zh-Hans',
                                                     password=password,is_setup=True,session=session)
                account.initialized_at=naive_utc_now()
                session.add(TenantAccountJoin(tenant_id=tenant_id,account_id=account.id,role=TenantAccountRole.NORMAL))
                session.commit()
                saved['accounts'].append({'email':email,'password':password,'id':account.id})
                output.write_text(json.dumps(saved,ensure_ascii=False,indent=2))
                os.chmod(output,0o600)
                os.chown(output,1000,1000)
        template=session.scalar(select(Agent).where(Agent.tenant_id==tenant_id,Agent.name=='工作台公共资源（开发验收）'))
        if template is None:
            existing=session.scalar(select(Agent).where(Agent.tenant_id==tenant_id,Agent.name=='test'))
            owner_account=session.get(Account,owner_id)
            owner_account.set_tenant_id_with_session(tenant_id,session=session)
            copied=AgentRosterService(session).duplicate_agent_app(tenant_id=tenant_id,agent_id=existing.id,
                account=owner_account,name='工作台公共资源（开发验收）')
            template=AgentRosterService(session).get_app_backing_agent(tenant_id=tenant_id,app_id=copied.id)
        if not template.active_config_is_published:
            snapshot=session.get(AgentConfigSnapshot,template.active_config_snapshot_id)
            soul=AgentSoulConfig.model_validate(snapshot.config_snapshot_dict).model_dump(mode='json')
            soul['prompt']['system_prompt']='你是工作台验收助手。遵循用户本次请求，使用本次配置中实际开放的工具。文件默认写入当前会话工作目录，共享文件放在 /workspace/shared。'
            soul['env']={'variables':[],'secret_refs':[]}
            soul['config_files']=[]
            soul['config_note']=''
            AgentComposerService.save_agent_composer(session=session,tenant_id=tenant_id,agent_id=template.id,
                account_id=owner_id,payload=ComposerSavePayload(variant='agent_app',save_strategy='save_to_current_version',
                agent_soul=AgentSoulConfig.model_validate(soul)))
            AgentComposerService.publish_agent_app_draft(session=session,tenant_id=tenant_id,agent_id=template.id,
                account_id=owner_id,version_note='工作台开发环境验收资源')
            session.commit()
        saved.update(tenant_id=tenant_id,agent_id=template.id)
        output.write_text(json.dumps(saved,ensure_ascii=False,indent=2))
        os.chmod(output,0o600)
        os.chown(output,1000,1000)
        print(json.dumps({'created_test_accounts':len(saved['accounts']),'tenant_id':tenant_id,'agent_id':template.id}))
