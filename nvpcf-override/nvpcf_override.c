// SPDX-License-Identifier: MIT
/* Correct ASUS PX13 NVPCF values before nvidia-powerd consumes them. */

#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/stat.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define NV0000_CTRL_CMD_SYSTEM_NVPCF_GET_POWER_MODE_INFO 0x13bU
#define NVPCF_GET_DYNAMIC_2X 3U
#define NVIDIA_RM_CONTROL_REQUEST 0xc020462aUL
#define CONFIG_PATH "/run/px13-powerd/nvpcf-override.conf"
#define BOARD_NAME_PATH "/sys/class/dmi/id/board_name"
#define SYS_VENDOR_PATH "/sys/class/dmi/id/sys_vendor"

struct nvos54_parameters {
	uint32_t client;
	uint32_t object;
	uint32_t command;
	uint32_t flags;
	uint64_t params;
	uint32_t params_size;
	int32_t status;
};

struct nvpcf_params_prefix {
	uint32_t supported_funcs;
	uint32_t gpu_id;
	uint32_t tpp;
	uint32_t rated_tgp;
	uint32_t subfunc;
	int32_t ctgp_offset_mw;
	int32_t target_tpp_offset_mw;
	int32_t max_output_offset_mw;
	int32_t min_output_offset_mw;
	int32_t ctgp_batt_offset_mw;
	int32_t target_tpp_batt_offset_mw;
	int32_t max_output_batt_offset_mw;
	int32_t min_output_batt_offset_mw;
	uint32_t dc_ros_reserve_override_mw;
	uint32_t dc_tsp_long_limit_ma;
	uint32_t dc_tsp_long_override_ma;
	uint32_t dc_tsp_short_limit_ma;
	uint32_t dc_tsp_short_override_ma;
	uint8_t enable_for_ac;
	uint8_t enable_for_dc;
	uint8_t version;
};

struct override_config {
	int enabled;
	int ctgp_offset_mw;
	int max_output_offset_mw;
};

typedef int (*real_ioctl_fn)(int, unsigned long, void *);

static real_ioctl_fn resolve_ioctl(void)
{
	static real_ioctl_fn function;

	if (!function)
		function = (real_ioctl_fn)dlsym(RTLD_NEXT, "ioctl");
	return function;
}

static bool read_config(struct override_config *config)
{
	char buffer[256] = { 0 };
	FILE *file;
	struct stat metadata;

	file = fopen(CONFIG_PATH, "re");
	if (!file)
		return false;
	if (fstat(fileno(file), &metadata) || metadata.st_uid != 0 ||
	    (metadata.st_mode & (S_IWGRP | S_IWOTH))) {
		fclose(file);
		return false;
	}
	if (!fread(buffer, 1, sizeof(buffer) - 1, file) && ferror(file)) {
		fclose(file);
		return false;
	}
	fclose(file);

	return sscanf(buffer,
		      "enabled=%d\nctgp_offset_mw=%d\nmax_output_offset_mw=%d",
		      &config->enabled, &config->ctgp_offset_mw,
		      &config->max_output_offset_mw) == 3;
}

static bool read_dmi_value(const char *path, char *value, size_t size)
{
	FILE *file = fopen(path, "re");

	if (!file)
		return false;
	if (!fgets(value, size, file))
		value[0] = '\0';
	fclose(file);
	value[strcspn(value, "\r\n")] = '\0';
	return value[0] != '\0';
}

static bool is_px13_board(void)
{
	static int cached_result = -1;
	char board_name[32] = { 0 };
	char sys_vendor[32] = { 0 };

	if (cached_result >= 0)
		return cached_result;

	if (!read_dmi_value(BOARD_NAME_PATH, board_name, sizeof(board_name)) ||
	    !read_dmi_value(SYS_VENDOR_PATH, sys_vendor, sizeof(sys_vendor))) {
		cached_result = 0;
		return false;
	}
	cached_result = strcmp(board_name, "HN7306WV") == 0 &&
		strcmp(sys_vendor, "ASUSTeK COMPUTER INC.") == 0;
	return cached_result;
}

static void override_nvpcf(unsigned long request, void *argument)
{
	static bool logged;
	struct nvos54_parameters *control = argument;
	struct nvpcf_params_prefix *params;
	struct override_config config;
	int old_ctgp;
	int old_max;

	if (request != NVIDIA_RM_CONTROL_REQUEST || !is_px13_board() || !control ||
	    control->command !=
			NV0000_CTRL_CMD_SYSTEM_NVPCF_GET_POWER_MODE_INFO ||
	    control->status != 0 || control->params_size < sizeof(*params) ||
	    !control->params)
		return;

	params = (struct nvpcf_params_prefix *)(uintptr_t)control->params;
	if (params->subfunc != NVPCF_GET_DYNAMIC_2X ||
	    params->version < 0x20 || params->version > 0x25 ||
	    !params->enable_for_ac || !read_config(&config) || !config.enabled)
		return;

	if ((config.enabled != 0 && config.enabled != 1) ||
	    config.ctgp_offset_mw < 0 || config.ctgp_offset_mw > 40000 ||
	    config.max_output_offset_mw < 0 ||
	    config.max_output_offset_mw > 40000 ||
	    config.ctgp_offset_mw + config.max_output_offset_mw > 40000)
		return;

	old_ctgp = params->ctgp_offset_mw;
	old_max = params->max_output_offset_mw;
	params->ctgp_offset_mw = config.ctgp_offset_mw;
	params->max_output_offset_mw = config.max_output_offset_mw;

	if (!logged) {
		dprintf(STDERR_FILENO,
			"PX13 NVPCF override: cTGP %d->%d mW, DB max %d->%d mW\n",
			old_ctgp, params->ctgp_offset_mw, old_max,
			params->max_output_offset_mw);
		logged = true;
	}
}

int ioctl(int fd, unsigned long request, ...)
{
	va_list arguments;
	void *argument;
	real_ioctl_fn real_ioctl;
	int result;

	va_start(arguments, request);
	argument = va_arg(arguments, void *);
	va_end(arguments);

	real_ioctl = resolve_ioctl();
	if (!real_ioctl) {
		errno = ENOSYS;
		return -1;
	}

	result = real_ioctl(fd, request, argument);
	if (result == 0)
		override_nvpcf(request, argument);
	return result;
}
