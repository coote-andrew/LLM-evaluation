# Phase A.1: ModelConfig gains is_agent + agent_alias to flag entries that
# point at the clinical_graphs agents service.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_modelconfig_max_concurrency'),
    ]

    operations = [
        migrations.AddField(
            model_name='modelconfig',
            name='is_agent',
            field=models.BooleanField(
                default=False,
                help_text=(
                    "If true, this endpoint is a clinical_graphs agent service exposing "
                    "pattern workflows via OpenAI-compatible /v1/chat/completions. "
                    "`model_name` should be the pattern alias (e.g. 'clinical_note_analysis')."
                ),
            ),
        ),
        migrations.AddField(
            model_name='modelconfig',
            name='agent_alias',
            field=models.SlugField(
                max_length=100,
                blank=True,
                null=True,
                unique=True,
                help_text=(
                    "Unique short name for this agent endpoint as referenced by Django UI "
                    "and by the generated llm_providers.yaml. Only meaningful when "
                    "is_agent=True. Leave blank for regular LLM configs."
                ),
            ),
        ),
    ]
