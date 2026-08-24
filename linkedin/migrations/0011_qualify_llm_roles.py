from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0010_linkedinprofile_user_fk"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="campaign",
            name="model_blob",
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="qualify_cheap_llm_provider",
            field=models.CharField(
                choices=[
                    ("openai", "OpenAI"),
                    ("anthropic", "Anthropic"),
                    ("google", "Google"),
                    ("groq", "Groq"),
                    ("mistral", "Mistral"),
                    ("cohere", "Cohere"),
                    ("openai_compatible", "OpenAI-compatible"),
                ],
                default="openai",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="qualify_cheap_llm_api_key",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="qualify_cheap_ai_model",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="qualify_cheap_llm_api_base",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="qualify_expensive_llm_provider",
            field=models.CharField(
                choices=[
                    ("openai", "OpenAI"),
                    ("anthropic", "Anthropic"),
                    ("google", "Google"),
                    ("groq", "Groq"),
                    ("mistral", "Mistral"),
                    ("cohere", "Cohere"),
                    ("openai_compatible", "OpenAI-compatible"),
                ],
                default="openai",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="qualify_expensive_llm_api_key",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="qualify_expensive_ai_model",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="siteconfig",
            name="qualify_expensive_llm_api_base",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]
