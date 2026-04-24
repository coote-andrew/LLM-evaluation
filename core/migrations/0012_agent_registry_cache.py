"""Metadata-only cache of the agents-service registry.

No Python source is stored here; the agents service remains the source of
truth for files on disk. See ``docs/AGENTS_SERVICE_GUIDE.md``.
"""

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_modelconfig_is_agent_and_alias'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentAsset',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(
                    choices=[
                        ('tool', 'Tool'),
                        ('node', 'Node'),
                        ('pattern', 'Pattern'),
                        ('system_prompt', 'System prompt'),
                    ],
                    max_length=20,
                )),
                ('name', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(default=True)),
                ('last_synced_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'ordering': ['kind', 'name'],
                'unique_together': {('kind', 'name')},
            },
        ),
        migrations.CreateModel(
            name='AgentAssetVersion',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('label', models.CharField(
                    help_text="E.g. '1.2' for a cut snapshot or '@latest' for the working copy.",
                    max_length=40,
                )),
                ('file_path', models.CharField(
                    blank=True, default='', max_length=500,
                    help_text='Path within the agents repo, for display only.',
                )),
                ('content_hash', models.CharField(
                    max_length=80,
                    help_text='sha256:<hex> of the file bytes as reported by the agents service.',
                )),
                ('git_sha', models.CharField(blank=True, default='', max_length=80)),
                ('declared_params', models.JSONField(blank=True, default=dict)),
                ('pinned_deps', models.JSONField(blank=True, default=dict)),
                ('is_working_copy', models.BooleanField(
                    default=False,
                    help_text='True for the synthetic @latest row tracking the working copy.',
                )),
                ('is_deprecated', models.BooleanField(default=False)),
                ('ready', models.BooleanField(
                    default=True,
                    help_text='False if the agents service reported a sandbox-import failure.',
                )),
                ('import_error', models.TextField(blank=True, default='')),
                ('created_at_agent', models.DateTimeField(
                    blank=True, null=True,
                    help_text='Timestamp reported by the agents service (source-of-truth).',
                )),
                ('first_seen_at', models.DateTimeField(auto_now_add=True)),
                ('last_synced_at', models.DateTimeField(blank=True, null=True)),
                ('asset', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='versions',
                    to='core.agentasset',
                )),
            ],
            options={
                'ordering': ['asset', '-label'],
                'unique_together': {('asset', 'label')},
            },
        ),
    ]
