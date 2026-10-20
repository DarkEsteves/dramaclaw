"""Material quality decisions remain protected from Agent credentials."""
import pytest
from fastapi import HTTPException
from novelvideo.api.routes.reference_material import material_confirm


@pytest.mark.asyncio
@pytest.mark.parametrize('credential_kind',['agent_session','local_trusted_agent'])
async def test_agent_cannot_confirm_material_content(credential_kind):
    with pytest.raises(HTTPException) as denied:
        await material_confirm('project','canvas','a'*32,{'credential_kind':credential_kind})
    assert denied.value.status_code == 403
