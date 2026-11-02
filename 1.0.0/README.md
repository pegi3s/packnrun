# This image belongs to a larger project called Bioinformatics Docker Images Project (http://pegi3s.github.io/dockerfiles)
## (Please note that the original software licenses still apply)

# packnrun

packnrun allows running Run an auto-pegi3s project (auto-phylo, auto-p2docking, auto-enrich, ...) on another computer, and get the results back.

Given an image, a project folder and the access details of a remote computer, packnrun:

1. Checks that the folder contains at least the `pipeline` and `config` files.
2. Copies the folder to the remote computer, by default to `~/packnrun/<folder>_<date>-<time>`.
3. Sets the `dir` variable of the remote `config` to the new path. Your original folder is not modified.
4. Runs the image in the remote computer.
5. When the run ends, copies the full folder back, next to the original, as `<folder>_results`.

## Remote computer requirements

- An SSH server and Docker usable by your user **without sudo**. The needed pegi3s images are downloaded automatically.

This can be achieved by running on the console of the remote computer the following commands (assuming the remote computer is running Ubuntu):

```
sudo apt update
sudo apt install -y openssh-server
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
```

Then log out and in again and confirm that `docker` works without `sudo` by running in the command line `docker run hello-world`. Members of the `docker` group have the equivalent of root access to that computer, so only add users you trust.

## Usage

Run the following commands from the folder that **contains** your project folder. That folder is mounted as `/data`, and `<project>_results` will be created there.

In the following examples, `-u $(id -u):$(id -g)` makes the results belong to you, instead of root. `-it` allows packnrun to ask for your password. `$PWD` captures the path.

### Example 1: a computer of your own (user and password)

To run the project in `my_project` on the computer `my_server`, as user `john_doe`:

`docker run --rm -it -u $(id -u):$(id -g) -v "$PWD":/data pegi3s/packnrun auto-phylo:latest my_project --host my_server --user john_doe`

where `latest` can be substituted by a specified version.

You are asked for the password, and the results end up in `my_project_results`.

To run without being asked for the password,give it as an environment variable:

`export PACKNRUN_PASSWORD='your password'`

and then

`docker run --rm -u $(id -u):$(id -g) -v "$PWD":/data -e PACKNRUN_PASSWORD pegi3s/packnrun auto-phylo:latest my_project --host ip_address --user john_doe`

where `latest` can be substituted by a specified version, and `ip_address` is the IP address of `my_server`

`--password 'your password'` also works, but it stays in your shell history and is visible to other users of the computer while packnrun runs.

### Example 2: an AWS server (user and SSH key)

If you set up an AWS server with Docker, you can also use packnrun to run auto jobs there. You must have a key file (e.g. `~/Downloads/my-key.pem`) and the public IP address of the server (e.g. `3.120.45.67`). The user of Ubuntu servers on AWS is always `ubuntu`. Then run:

`docker run --rm -it -u $(id -u):$(id -g) -v "$PWD":/data -v ~/Downloads/my-key.pem:/key.pem:ro pegi3s/packnrun auto-p2docking:latest my_project --host 3.120.45.67 --user ubuntu --key /key.pem`

Note that `--key` uses the path of the key **inside** the container (`/key.pem`).

### Options

| Option | Description |
| --- | --- |
| `--host` | Name or IP address of the remote computer (required) |
| `--user` | User on the remote computer (required) |
| `--password` | Password, or passphrase of the key |
| `--key` | Private SSH key file, such as the `.pem` file of an AWS key pair |
| `--port` | SSH port (default: 22) |
| `--remote-dir` | Absolute remote folder for the runs (default: `~/packnrun`) |
| `--cleanup` | Delete the remote copy after the results are copied back. By default it is kept |
| `--known-hosts` | `known_hosts` file used to verify the remote computer; unknown computers are refused |
| `--reconnect-timeout` | Seconds to keep trying to reconnect after losing the connection (default: 3600) |

The image can be written with or without the `pegi3s/` prefix. Without a version, `latest` is used.

## Good to know

- **Choosing the version.** Old versions of the auto images only work with certain Docker versions, as described in their Docker Hub pages. packnrun prints the Docker version of the remote computer. A recently created server has a recent Docker.
- **A failed pipeline** still has its folder copied back, so that you can read the logs. packnrun then exits with an error.
- **Lost connections** do not stop the run: it continues on the remote computer and packnrun reconnects.
- **Closing packnrun or turning off your computer** does not stop the run either, but nothing is then copied back. packnrun prints the name of the remote container and how to stop it. To get the results, wait for the run to end, and copy the remote folder yourself.
- **Existing results are never overwritten.** If `<folder>_results` or `<folder>_results.partial` exist, packnrun stops before doing anything. A `.partial` folder means that copying the results back failed. The message says where the complete results are.
- **The `config` in the results** is the one used in the run, so its `dir` has the remote path. This does not matter to run it again with packnrun, which always sets `dir`. To run it locally, set `dir` to the local path.
- **Remote computer identity.** On the first connection to a computer, packnrun accepts its identity and prints a warning with its fingerprint, since a new AWS server is always unknown. To have it verified instead, mount a `known_hosts` file and use `--known-hosts`.
