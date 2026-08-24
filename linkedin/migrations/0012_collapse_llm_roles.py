from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0011_qualify_llm_roles"),
    ]

    operations = [
        # chat_* and task_* collapse into one general-purpose "expensive" role.
        migrations.RemoveField(model_name="siteconfig", name="chat_llm_provider"),
        migrations.RemoveField(model_name="siteconfig", name="chat_llm_api_key"),
        migrations.RemoveField(model_name="siteconfig", name="chat_ai_model"),
        migrations.RemoveField(model_name="siteconfig", name="chat_llm_api_base"),
        migrations.RemoveField(model_name="siteconfig", name="task_llm_provider"),
        migrations.RemoveField(model_name="siteconfig", name="task_llm_api_key"),
        migrations.RemoveField(model_name="siteconfig", name="task_ai_model"),
        migrations.RemoveField(model_name="siteconfig", name="task_llm_api_base"),
        # qualify_expensive_* was the same role the new "expensive" field group
        # now covers more broadly — renamed rather than dropped so any value
        # already configured there survives the upgrade.
        migrations.RenameField(model_name="siteconfig", old_name="qualify_expensive_llm_provider", new_name="expensive_llm_provider"),
        migrations.RenameField(model_name="siteconfig", old_name="qualify_expensive_llm_api_key", new_name="expensive_llm_api_key"),
        migrations.RenameField(model_name="siteconfig", old_name="qualify_expensive_ai_model", new_name="expensive_ai_model"),
        migrations.RenameField(model_name="siteconfig", old_name="qualify_expensive_llm_api_base", new_name="expensive_llm_api_base"),
        # qualify_cheap_* is renamed since it keeps the same single purpose
        # (the qualification prefilter) under a shorter name.
        migrations.RenameField(model_name="siteconfig", old_name="qualify_cheap_llm_provider", new_name="cheap_llm_provider"),
        migrations.RenameField(model_name="siteconfig", old_name="qualify_cheap_llm_api_key", new_name="cheap_llm_api_key"),
        migrations.RenameField(model_name="siteconfig", old_name="qualify_cheap_ai_model", new_name="cheap_ai_model"),
        migrations.RenameField(model_name="siteconfig", old_name="qualify_cheap_llm_api_base", new_name="cheap_llm_api_base"),
    ]
