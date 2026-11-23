# Observatory

Lets asterism run tasks on [Observatory](https://github.com/asterism-dev/observatory) VMs: on an existing VM or on a new one asterism creates, hibernates when its tasks are archived, resumes on restore and deletes with its last task.

Requires the `observatory` CLI on your `PATH`, logged in (`observatory login`), with your SSH key registered (`observatory ssh-key add`) and a WireGuard device up, so `ssh dev@<vm>.internal` works. The org is the CLI's default org.

The settings are the defaults of the **New VM…** dialog: image (empty uses your default image), CPUs, RAM and disk in GB.
