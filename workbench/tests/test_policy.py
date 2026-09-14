import copy
import pytest
from services.workbench.policy import Selection, compile_selection, prune_unavailable_selection, public_resources, resource_key


@pytest.fixture
def template():
    return {'model':{}, 'tools':{'dify_tools':[{'provider_type':'mcp','provider_id':'fixture','tool_name':'read',
        'enabled':True,'runtime_parameters':{'secret':'private-value','limit':3}}], 'cli_tools':[{'command':'private'}]},
        'config_skills':[{'name':'alpha','file_id':'skill-1'},{'name':'beta','file_id':'skill-2'}],
        'knowledge':{'sets':[{'id':'kb-1','name':'Knowledge 1'}]},
        'env':{'variables':[{'name':'SECRET','value':'private-value'}]},
        'config_files':[{'name':'guide.md','file_kind':'upload_file','file_id':'published-file'}]}


def test_selection_is_frozen_and_only_exposes_selected_resources(template):
    original=copy.deepcopy(template)
    key=resource_key(template['tools']['dify_tools'][0])
    selection=Selection(model='m',tools=[key],skills=['beta'],knowledge=['kb-1'],tool_parameters={key:{'limit':4}})
    result=compile_selection(template,selection,{'m':{'model':'m'}},tool_parameter_rules={key:{'limit':{'type':'integer','maximum':10}}})
    assert template==original
    assert [skill['name'] for skill in result['config_skills']]==['beta']
    assert result['tools']['dify_tools'][0]['runtime_parameters']['limit']==4
    assert result['tools']['cli_tools']==[] and result['env']=={'variables':[],'secret_refs':[]}
    assert result['config_files']==original['config_files']
    template['tools']['dify_tools'][0]['tool_name']='changed'
    template['config_files'][0]['file_id']='replacement-file'
    assert result['tools']['dify_tools'][0]['tool_name']=='read'
    assert result['config_files'][0]['file_id']=='published-file'


@pytest.mark.parametrize('changes',[
    {'model':'forged'}, {'skills':['forged']}, {'knowledge':['forged']}, {'tools':['forged']},
    {'skills':['alpha','alpha']}, {'model_parameters':{'api_key':'forged'}},
    {'config_files':[{'name':'forged.md','file_kind':'upload_file','file_id':'forged'}]},
])
def test_forged_selection_rejected(template,changes):
    with pytest.raises(ValueError):
        compile_selection(template,Selection(**({'model':'m'}|changes)),{'m':{'model':'m'}})


def test_public_tool_parameters_require_explicit_allowlist(template):
    key=resource_key(template['tools']['dify_tools'][0])
    assert public_resources(template)['tools'][0]['parameters']=={}
    public=public_resources(template,{key:{'limit':{'type':'integer'}}})
    assert public['tools'][0]['parameters']=={'limit':{'value':3,'schema':{'type':'integer'}}}
    assert 'private-value' not in str(public)
    with pytest.raises(ValueError):
        compile_selection(template,Selection(model='m',tools=[key],tool_parameters={key:{'secret':'replacement'}}),{'m':{'model':'m'}})


@pytest.mark.parametrize('value',[float('nan'),float('inf'),True,-1,11,'bad'])
def test_model_parameter_limits(template,value):
    with pytest.raises(ValueError):
        compile_selection(template,Selection(model='m',model_parameters={'temperature':value}),{'m':{'model':'m'}},
                          {'temperature':{'type':'float','min':0,'max':10}})


def test_inherited_defaults_drop_withdrawn_resources_without_enabling_new_ones(template):
    key=resource_key(template['tools']['dify_tools'][0])
    selected=Selection(model='unchanged',tools=[key,'withdrawn'],skills=['alpha','old'],knowledge=['old-kb'],tool_parameters={key:{'limit':4},'withdrawn':{'limit':8}})
    clean=prune_unavailable_selection(template,selected)
    assert clean.tools==[key] and clean.skills==['alpha'] and clean.knowledge==[]
    assert clean.tool_parameters=={key:{'limit':4}} and clean.model=='unchanged'
    assert selected.tools==[key,'withdrawn']
