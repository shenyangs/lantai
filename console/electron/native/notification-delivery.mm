#include <node_api.h>
#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>

#include <chrono>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace {
struct Snapshot {
  std::string authorization = "unknown";
  std::vector<std::string> delivered_ids;
};

struct ReadState {
  std::mutex mutex;
  std::condition_variable ready;
  bool settings_received = false;
  bool notifications_received = false;
  Snapshot snapshot;
};

struct Work {
  napi_async_work task = nullptr;
  napi_deferred deferred = nullptr;
  Snapshot result;
};

const char* AuthorizationName(UNAuthorizationStatus status) {
  switch (status) {
    case UNAuthorizationStatusAuthorized: return "authorized";
    case UNAuthorizationStatusProvisional: return "provisional";
    case UNAuthorizationStatusDenied: return "denied";
    case UNAuthorizationStatusNotDetermined: return "not_determined";
    default: return "unknown";
  }
}

void Execute(napi_env, void* data) {
  auto* work = static_cast<Work*>(data);
  // Apple callbacks may outlive the bounded worker read. They own only this
  // shared state, never the Work or a JS environment that may have closed.
  const auto state = std::make_shared<ReadState>();
  @autoreleasepool {
    @try {
      UNUserNotificationCenter* center = [UNUserNotificationCenter currentNotificationCenter];
      [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings* settings) {
        {
          std::lock_guard<std::mutex> lock(state->mutex);
          state->snapshot.authorization = AuthorizationName(settings.authorizationStatus);
          state->settings_received = true;
        }
        state->ready.notify_one();
      }];
      [center getDeliveredNotificationsWithCompletionHandler:^(NSArray<UNNotification*>* notifications) {
        std::vector<std::string> identifiers;
        for (UNNotification* notification in notifications) {
          const char* identifier = notification.request.identifier.UTF8String;
          if (identifier && identifier[0]) identifiers.emplace_back(identifier);
        }
        {
          std::lock_guard<std::mutex> lock(state->mutex);
          state->snapshot.delivered_ids = std::move(identifiers);
          state->notifications_received = true;
        }
        state->ready.notify_one();
      }];
      std::unique_lock<std::mutex> lock(state->mutex);
      if (state->ready.wait_for(lock, std::chrono::milliseconds(500), [&] {
            return state->settings_received && state->notifications_received;
          })) {
        work->result = state->snapshot;
      }
    } @catch (NSException*) {
      // Unavailable notification services are unknown, never a success receipt.
    }
  }
}

napi_value ToValue(napi_env env, const Snapshot& snapshot) {
  napi_value value, authorization, identifiers;
  if (napi_create_object(env, &value) != napi_ok ||
      napi_create_string_utf8(env, snapshot.authorization.c_str(), NAPI_AUTO_LENGTH, &authorization) != napi_ok ||
      napi_create_array_with_length(env, snapshot.delivered_ids.size(), &identifiers) != napi_ok) return nullptr;
  for (size_t i = 0; i < snapshot.delivered_ids.size(); ++i) {
    napi_value identifier;
    if (napi_create_string_utf8(env, snapshot.delivered_ids[i].c_str(), NAPI_AUTO_LENGTH, &identifier) != napi_ok ||
        napi_set_element(env, identifiers, i, identifier) != napi_ok) return nullptr;
  }
  if (napi_set_named_property(env, value, "authorization", authorization) != napi_ok ||
      napi_set_named_property(env, value, "deliveredIds", identifiers) != napi_ok) return nullptr;
  return value;
}

void Complete(napi_env env, napi_status status, void* data) {
  std::unique_ptr<Work> work(static_cast<Work*>(data));
  if (env) {
    napi_value value = ToValue(env, status == napi_ok ? work->result : Snapshot{});
    if (value) napi_resolve_deferred(env, work->deferred, value);
    napi_delete_async_work(env, work->task);
  }
}

napi_value ReadSnapshot(napi_env env, napi_callback_info) {
  auto work = std::make_unique<Work>();
  napi_value promise, name;
  if (napi_create_promise(env, &work->deferred, &promise) != napi_ok ||
      napi_create_string_utf8(env, "lantai:notification-delivery-read", NAPI_AUTO_LENGTH, &name) != napi_ok) return nullptr;
  if (napi_create_async_work(env, nullptr, name, Execute, Complete, work.get(), &work->task) != napi_ok ||
      napi_queue_async_work(env, work->task) != napi_ok) {
    if (work->task) napi_delete_async_work(env, work->task);
    napi_value unknown = ToValue(env, Snapshot{});
    if (unknown) napi_resolve_deferred(env, work->deferred, unknown);
    return promise;
  }
  work.release();
  return promise;
}

napi_value Init(napi_env env, napi_value exports) {
  napi_value read;
  if (napi_create_function(env, "readSnapshot", NAPI_AUTO_LENGTH, ReadSnapshot, nullptr, &read) != napi_ok ||
      napi_set_named_property(env, exports, "readSnapshot", read) != napi_ok) return nullptr;
  return exports;
}
}  // namespace

NAPI_MODULE(lantai_notification_delivery, Init)
