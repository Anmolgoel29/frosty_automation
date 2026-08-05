from django.db import migrations, models

_PROVIDER_CHOICES = [
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("google", "Google"),
    ("groq", "Groq"),
    ("mistral", "Mistral"),
    ("cohere", "Cohere"),
    ("openai_compatible", "OpenAI-compatible"),
]


def copy_existing_config_into_chat_and_task(apps, schema_editor):
    """Seed the new `chat_*` / `task_*` fields from the old single model config.

    Existing installs only ever configured one model. Copy it into both roles
    so the daemon keeps working immediately after upgrade; admins can later
    split them apart (e.g. a cheaper model for `task_*`) via Django Admin.
    """
    SiteConfig = apps.get_model("linkedin", "SiteConfig")
    SiteConfig.objects.filter(llm_api_key__gt="").update(
        chat_llm_provider=models.F("llm_provider"),
        chat_llm_api_key=models.F("llm_api_key"),
        chat_ai_model=models.F("ai_model"),
        chat_llm_api_base=models.F("llm_api_base"),
        task_llm_provider=models.F("llm_provider"),
        task_llm_api_key=models.F("llm_api_key"),
        task_ai_model=models.F("ai_model"),
        task_llm_api_base=models.F("llm_api_base"),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0007_siteconfig_llm_provider"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfig",
            name="chat_llm_provider",
            field=models.CharField(choices=_PROVIDER_CHOICES, default="openai", max_length=32),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="chat_llm_api_key",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="chat_ai_model",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="chat_llm_api_base",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="task_llm_provider",
            field=models.CharField(choices=_PROVIDER_CHOICES, default="openai", max_length=32),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="task_llm_api_key",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="task_ai_model",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="task_llm_api_base",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.RunPython(
            copy_existing_config_into_chat_and_task,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(model_name="siteconfig", name="llm_provider"),
        migrations.RemoveField(model_name="siteconfig", name="llm_api_key"),
        migrations.RemoveField(model_name="siteconfig", name="ai_model"),
        migrations.RemoveField(model_name="siteconfig", name="llm_api_base"),
    ]
